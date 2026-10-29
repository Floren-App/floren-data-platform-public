"""Runs: programs from fdp.json (or ad-hoc files) on sized warehouses, with console and outputs."""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Literal, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import User, mint_token
from ..access_errors import data_access_issue
from ..config import get_settings
from ..db import get_db
from ..deps import get_actor, get_orchestrator, get_secret_store, granted_sources
from ..identity import SA_PREFIX, SecretAccessError, authorize_secrets, get_sa, is_sa, mask, secret_env_from_paths
from ..models import ComputePool, Run, Workspace, utcnow
from .. import capacity, rbac
from ..orchestrator import Orchestrator
from ..programs import WORKSPACE, ArgError, Program, is_safe_relpath
from ..runtimes import LABEL as RUNTIME_LABEL
from ..runtimes import RuntimeUnavailable
from ..runtimes import resolve as resolve_runtime
from ..schemas import OpenOut, OutputContent, OutputFile, RunIn, RunOut
from ..executions import RunPlan, plan_from_program, run_spec, runtime_for, safe_entrypoint
from ..workspace_files import load_config, outputs, read_output
from ..workspace_files import console as run_console
from .common import audit, run_out

router = APIRouter(prefix="/v1/runs", tags=["runs"])
ACTIVE = ("pending", "running")
PREVIEW_LIMIT = 5 * 1024 * 1024
STOP_GRACE_SECONDS = 5


def owned_run(db: Session, run_id: str, user: User) -> Run:
    """Your own runs, and runs of service accounts you may use (e.g. ones Airflow started)."""
    run = db.get(Run, run_id)
    from ..environments import run_permitted
    if run and not run_permitted(db, user, run):
        raise HTTPException(404, "run not found")
    if run and user.is_admin:
        return run
    if run and run.workspace_id and not run.workload_id:
        ws = db.get(Workspace, run.workspace_id)  # workspace runs are private to the workspace's owner
        if ws is None or not rbac.same_identity(ws, user):
            raise HTTPException(404, "run not found")
    if run and (rbac.same_identity(run, user) or (run.initiator_id == user.principal_id and user.principal_id) or
                (not run.initiator_id and run.initiated_by == user.username)):
        return run
    if run and is_sa(run.owner):
        sa = get_sa(db, run.owner[len(SA_PREFIX):])
        if sa and rbac.allowed(db, user, "edit", rbac.service_account_resource(db, sa)):
            return run
    raise HTTPException(404, "run not found")


@router.get("", response_model=List[RunOut])
def list_runs(workspace_id: str = "", limit: int = Query(100, ge=1, le=500),
              sort: Literal["created", "updated"] = "created",
              scope: Literal["", "deployed", "workspace"] = "",
              db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Your most recent runs, plus those of service accounts you may use (e.g. started by a deployed Airflow).
    scope: "deployed" for runs of deployed programs, "workspace" for runs started in workspaces."""
    from ..models import ServiceAccount, Workload

    owners = [user.username] + [f"{SA_PREFIX}{sa.name}" for sa in db.scalars(select(ServiceAccount))
                                if not is_sa(user.username) and rbac.allowed(db, user, "edit", rbac.service_account_resource(db, sa))]
    stmt = select(Run).where((Run.principal_id == user.principal_id) | (Run.initiator_id == user.principal_id) |
        (Run.principal_id.is_(None) & Run.owner.in_(owners)) | Run.owner.in_(owners[1:]) |
        (Run.initiator_id.is_(None) & (Run.initiated_by == user.username)))
    if user.is_admin:
        stmt = select(Run)
    if workspace_id:
        stmt = stmt.where(Run.workspace_id == workspace_id)
    if scope == "deployed":
        stmt = stmt.where(Run.workload_id.is_not(None))
    elif scope == "workspace":
        stmt = stmt.where(Run.workload_id.is_(None), Run.workspace_id.is_not(None))
    if not user.is_admin:  # workspace runs are private to the workspace's owner (and administrators)
        own = select(Workspace.id).where((Workspace.principal_id == user.principal_id) |
                                         (Workspace.principal_id.is_(None) & (Workspace.owner == user.username)))
        stmt = stmt.where(Run.workspace_id.is_(None) | Run.workload_id.is_not(None) | Run.workspace_id.in_(own))
    from ..ownership import browsing_allowed
    visible_workloads = [p.id for p in db.scalars(select(Workload))
                            if browsing_allowed(db,user,rbac.workload_resource(db,p))]
    stmt = stmt.where(Run.workload_id.is_(None) | Run.workload_id.in_(visible_workloads))
    from ..environments import run_permitted
    ordered_at = func.coalesce(Run.finished_at, Run.created_at) if sort == "updated" else Run.created_at
    return [run_out(r) for r in db.scalars(stmt.order_by(ordered_at.desc(), Run.id.desc()).limit(limit)) if run_permitted(db, user, r)]


def _plan(body: RunIn, db: Session, orch: Orchestrator, ws: Workspace, user: User, granted: set):
    """Resolves the request into what to run and on which warehouse."""
    if body.program is None:
        entry = safe_entrypoint(body.entrypoint or "")
        rt = runtime_for(entry)
        kind = body.kind or "script"
        return (RunPlan(kind=kind, entrypoint=entry, runtime=rt, image_kind=rt),
                capacity.choose_warehouse(db, body.pool, None, _layer_of(db, ws), kind))

    cfg_out, programs = load_config(db, orch, ws)
    if not cfg_out.exists:
        raise HTTPException(422, "this workspace has no fdp.json")
    resolved = programs.get(body.program)
    if resolved is None:
        if cfg_out.errors:
            raise HTTPException(422, {"message": "fdp.json is invalid", "errors": cfg_out.errors})
        raise HTTPException(404, f"program {body.program!r} is not available in this workspace's layer")
    plan = checked_plan(body.program, resolved.program, body.args, granted, db=db, actor=user)
    plan.workdir = f"{WORKSPACE}/{resolved.folder}" if resolved.folder else WORKSPACE
    return plan, capacity.choose_warehouse(db, body.pool, resolved.program.warehouse, _layer_of(db, ws), plan.kind)


def _layer_of(db, ws: Workspace) -> Optional[str]:
    from ..layers import canonical
    return canonical(db, ws.environment_id)


def checked_plan(name: str, program: Program, values: Dict[str, Any], granted: Set[str], *,
                 db: Session, actor: User, viewer: Optional[User] = None) -> RunPlan:
    from ..layers import require_program_layer
    require_program_layer(db, program, actor.environment_id, status_code=403)
    missing = [d for d in program.data_sources if d not in granted]
    if missing:
        issue = data_access_issue(db, actor, missing, viewer or actor)
        issue["message"] = f'Cannot run "{name}". ' + issue["message"]
        raise HTTPException(403, issue)
    try:
        return plan_from_program(program, values, granted)
    except ArgError as exc:
        raise HTTPException(422, {"message": "invalid arguments", "errors": exc.errors}) from None


def start_run(db: Session, orch: Orchestrator, user: User, plan: RunPlan, pool_name: Optional[str],
              workspace_id: Optional[str], program: Optional[str], initiated_by: Optional[str] = None, initiator_id: Optional[str] = None) -> Run:
    """Common to workspace and deployed runs: admission (access, secrets, capacity), then launch or queue."""
    from ..environments import bind
    from ..models import Workload
    if workspace_id:
        ws_context = db.get(Workspace, workspace_id)
        from dataclasses import replace
        from ..layers import canonical
        user = replace(user, environment_id=canonical(db, ws_context.environment_id), execution_context=f"workspace:{workspace_id}")
    elif plan.workload_id:
        user = bind(db, user, rbac.workload_resource(db, db.get(Workload, plan.workload_id)))
    from ..layer_infrastructure import require_ready
    require_ready(db, user.environment_id)
    if not pool_name:
        raise HTTPException(422, "choose a warehouse (no default warehouse is configured; an administrator can set one)")
    pool = db.scalar(select(ComputePool).where(ComputePool.name == pool_name))
    if not pool or (pool.allowed_groups and not set(pool.allowed_groups) & set(user.groups)):
        raise HTTPException(404, f"warehouse {pool_name!r} not available")
    capacity.refuse_trino_warehouse(db, pool.name)
    capacity.require_scope(db, pool, user.environment_id, plan.kind)  # its layers and kinds of work

    try:
        rt = resolve_runtime(db, plan.image_kind, plan.version)
    except RuntimeUnavailable as exc:
        raise HTTPException(422, str(exc)) from None
    plan.image, plan.version = rt.image, rt.version
    var_paths: Dict[str, str] = {}
    if plan.secret_refs:
        try:
            var_paths = authorize_secrets(db, get_secret_store(), user, plan.secret_refs)
        except SecretAccessError as exc:
            raise HTTPException(403, f"secrets: {exc}") from None

    from ..data_access import snapshot
    from ..rbac import resolve_user
    user = resolve_user(db, user)
    from ..ownership import creation_data_snapshot
    if plan.workload_id:
        from ..sla_monitor import require_inputs
        require_inputs(db, db.get(Workload, plan.workload_id), user)
    # Admissions to one warehouse are serialized, so two requests cannot both take its last slot.
    pool = db.scalar(select(ComputePool).where(ComputePool.id == pool.id).with_for_update())
    wait = bool(capacity.queue(db, pool.name)) or capacity.shortfall(db, {pool.name: 1}) is not None
    run = Run(owner=user.username, principal_id=user.principal_id, initiator_id=initiator_id or user.principal_id, initiated_by=initiated_by or user.username, access_snapshot=snapshot(db, user), environment_id=user.environment_id or None, workspace_id=workspace_id, workload_id=plan.workload_id,
              primary_create_snapshot=creation_data_snapshot(db,user),
              primary_group_id=user.active_group_id or None,
              commit_sha=plan.commit_sha, pool_id=pool.id, kind=plan.kind, entrypoint=plan.entrypoint,
              program=program, args=plan.args, runtime=plan.image_kind, runtime_version=rt.version,
              status="queued" if wait else "pending")
    run.pool = pool
    db.add(run)
    db.flush()
    sources = granted_sources(db, user)
    if workspace_id:
        ws = db.get(Workspace, workspace_id)
        plan.home_owner = ws.volume_owner or ws.owner
    if wait:
        # Starts when the warehouse has room, in arrival order. Secrets are kept as paths, never values.
        run.queued_at = utcnow()
        run.launch = {"plan": plan_record(plan), "secrets": var_paths, "groups": list(user.groups),
                      "sources": [d.name for d in sources], "program": program}
        audit(db, user.username, "run.queue", run.id, pool=pool.name, kind=run.kind, program=program)
        db.commit()
        return run
    plan.secret_env, plan.secret_paths = secret_env_from_paths(get_secret_store(), var_paths)
    run.secret_paths = plan.secret_paths
    db.commit()  # admit and persist the immutable snapshot before the container can call back
    launch(db, orch, run, pool, plan, user.groups, sources, program)
    return run


def plan_record(plan: RunPlan) -> dict:
    """A run plan as JSON for the queue, without resolved secret values."""
    from dataclasses import fields
    record = {f.name: getattr(plan, f.name) for f in fields(plan) if f.name != "secret_env"}
    if plan.dbt is not None:
        record["dbt"] = plan.dbt.model_dump(by_alias=True)
    return record


def plan_from_record(record: dict) -> RunPlan:
    from ..programs import DbtSettings
    data = dict(record)
    if data.get("dbt") is not None:
        config = dict(data["dbt"])
        # Plans admitted before aliases were preserved are already durable.
        if "schema_" in config:
            config.setdefault("schema", config.pop("schema_"))
        data["dbt"] = DbtSettings(**config)
    return RunPlan(**data)


def launch(db: Session, orch: Orchestrator, run: Run, pool: ComputePool, plan: RunPlan, groups: List[str],
           sources, program: Optional[str]) -> None:
    who = f"program {program}" if program else f"{plan.kind} {plan.entrypoint}"
    if plan.commit_sha:
        who += f" (deployed, commit {plan.commit_sha[:12]})"
    plan.label = (f"{who} on warehouse {pool.name} ({pool.cpu:g} vCPU, {pool.memory_gb:g} GB), "
                  f"{RUNTIME_LABEL[plan.image_kind]} {run.runtime_version or plan.version}")
    try:
        from ..artifacts import place
        handle = orch.start(place(db, run_spec(get_settings(), run, pool, plan, groups, sources),
                                  run.environment_id, orch))
    except Exception as exc:
        run.status, run.finished_at = "failed", utcnow()
        db.commit()
        from ..orchestrator.base import CapacityError
        if isinstance(exc, CapacityError):
            raise HTTPException(409, {"code": "LayerQuotaExceeded", "message": str(exc)}) from None
        raise
    run.execution_ref, run.upstream_url, run.status = handle.ref, handle.upstream_url, "running"
    audit(db, run.owner, "run.start", run.id, pool=pool.name, kind=run.kind, entrypoint=run.entrypoint,
          program=program, args=plan.args, workload=plan.workload_id, commit=plan.commit_sha)
    db.commit()


def launch_queued_run(db: Session, orch: Orchestrator, run: Run) -> bool:
    """The dispatcher's half of start_run: launch an admitted run now that its warehouse has room."""
    import logging
    from ..models import DataSource
    data = run.launch or {}
    plan = plan_from_record(data.get("plan") or {})
    try:
        plan.secret_env, plan.secret_paths = secret_env_from_paths(get_secret_store(), data.get("secrets") or {})
    except SecretAccessError as exc:
        logging.getLogger(__name__).warning("queued run %s cannot start: %s", run.id, exc)
        run.status, run.finished_at, run.launch = "failed", utcnow(), None
        audit(db, run.owner, "run.fail", run.id, reason="secret unavailable when the run left the queue")
        db.commit()
        return False
    names = data.get("sources") or []
    sources = list(db.scalars(select(DataSource).where(DataSource.name.in_(names)))) if names else []
    run.status, run.launch, run.secret_paths = "pending", None, plan.secret_paths
    db.commit()
    try:
        launch(db, orch, run, run.pool, plan, data.get("groups") or [], sources, data.get("program"))
    except Exception:
        logging.getLogger(__name__).exception("queued run %s could not start", run.id)
        return False
    return True


@router.post("", response_model=RunOut, status_code=201)
def create_run(
    body: RunIn,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
    user: User = Depends(get_actor),
):
    ws = db.get(Workspace, body.workspace_id)
    if not ws or not rbac.same_identity(ws, user):
        raise HTTPException(404, "workspace not found")
    from ..environments import execution_user, workspace_environment
    if user.execution_context and user.execution_context != f"workspace:{ws.id}":
        raise HTTPException(403, "cannot start work in another workspace")
    workspace_environment(db, user, ws.environment_id)
    primary = user.active_group_id
    user = rbac.resolve_user(db, execution_user(db, {"sub": ws.owner, "pid": ws.principal_id, "ws": ws.id}))
    from dataclasses import replace
    user = replace(user,active_group_id=primary)
    granted = {s.name for s in granted_sources(db, user)}
    plan, pool_name = _plan(body, db, orch, ws, user, granted)
    return run_out(start_run(db, orch, user, plan, pool_name, ws.id, body.program))


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    return run_out(owned_run(db, run_id, user))


@router.get("/{run_id}/logs", response_class=PlainTextResponse)
def run_logs(
    run_id: str,
    tail: int = 2000,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
    user: User = Depends(get_actor),
):
    """The program's console: live while it runs, from console.log after its container is gone."""
    run = owned_run(db, run_id, user)
    text = run_console(orch, (db.get(Workspace, run.workspace_id) if run.workspace_id else None), run,
                       tail=max(1, min(tail, 20000)))
    return mask(text, run.id, run.secret_paths or [], get_secret_store())


@router.get("/{run_id}/outputs", response_model=List[OutputFile])
def run_outputs(
    run_id: str,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
    user: User = Depends(get_actor),
):
    """Files the program wrote to $FDP_OUTPUT_DIR."""
    run = owned_run(db, run_id, user)
    files = outputs(orch, (db.get(Workspace, run.workspace_id) if run.workspace_id else None), run)
    if files is None:
        raise HTTPException(409, "start the workspace to browse outputs (they live on its volume)")
    return [OutputFile(path=p, size=s) for p, s in files]


@router.get("/{run_id}/outputs/{path:path}", response_model=OutputContent)
def run_output(
    run_id: str,
    path: str,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
    user: User = Depends(get_actor),
):
    """Preview of one output file (first 5 MB) for display in the hub."""
    if not is_safe_relpath(path):
        raise HTTPException(422, "invalid path")
    run = owned_run(db, run_id, user)
    ws = (db.get(Workspace, run.workspace_id) if run.workspace_id else None)
    sizes = dict(outputs(orch, ws, run) or [])
    if path not in sizes:
        raise HTTPException(404, "output not found")
    data = read_output(orch, ws, run, path, PREVIEW_LIMIT) or b""
    audit(db, user.username, "run.output.view", run.id, path=path)
    db.commit()
    return OutputContent(path=path, size=sizes[path], truncated=sizes[path] > len(data),
                         content_base64=base64.b64encode(data).decode())


@router.post("/{run_id}/stop", response_model=RunOut)
def stop_run(
    run_id: str,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
    user: User = Depends(get_actor),
):
    """Stop button: the program gets SIGTERM and a few seconds to clean up before it is killed."""
    run = owned_run(db, run_id, user)
    if run.execution_ref:
        orch.stop(run.execution_ref, grace_seconds=STOP_GRACE_SECONDS if run.status in ACTIVE else 0)
    if run.status in ACTIVE or run.status == "queued":
        run.status, run.finished_at, run.launch = "stopped", utcnow(), None
        audit(db, user.username, "run.stop", run.id)
    run.execution_ref = run.upstream_url = None
    db.commit()
    return run_out(run)


@router.post("/{run_id}/open", response_model=OpenOut)
def open_app(run_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    run = owned_run(db, run_id, user)
    if run.kind not in ("app", "airflow") or run.status != "running":
        raise HTTPException(409, "only running apps and Airflow test deployments can be opened")
    ticket = mint_token("ticket", user.username, 60, target=f"apps:{run.id}")
    return OpenOut(url=f"{get_settings().public_url}/apps/{run.id}/?dc_ticket={ticket}")
