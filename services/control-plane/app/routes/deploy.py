"""Git repositories (admin), the Deploy flow, the catalog of deployed programs and their runs."""

from __future__ import annotations

from .. import layers

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from ..auth import User, get_execution, Execution, mint_token, require_admin
from ..access_errors import data_access_issue
from ..config import get_settings
from ..db import SessionLocal, get_db
from ..deps import get_actor, get_orchestrator, get_secret_store, granted_sources
from ..git import GitError, valid_branch
from ..identity import as_user, get_sa, may_use
from ..models import GitRepository, Workload, Resource, DirectoryPrincipal
from ..orchestrator import Orchestrator
from ..ownership import owner_info, browsing_allowed
from ..secrets import SecretStore, repo_path
from .. import deploying, rbac
from ..schemas import (BranchOut, CommitOut, OpenOut, OrchestrateIn, WorkloadEventOut, WorkloadOut,
                       WorkloadPatch, DeployedRunIn, DeployIn, PullOut, RepoIn, RepoOut, RunOut)
from .common import audit, run_out
from .runs import checked_plan, start_run

router = APIRouter(prefix="/v1", tags=["deploying"])


# --- repositories ----------------------------------------------------------------------------

def _repo_out(repo: GitRepository, secrets: SecretStore) -> RepoOut:
    out = RepoOut.model_validate(repo)
    out.has_credentials = bool(secrets.read(repo_path(repo.name)))
    return out


def _can_deploy_from(repo: GitRepository, user: User, db: Optional[Session] = None) -> bool:
    from .. import rbac
    groups = set(user.groups) | (rbac.app_groups(db,user) if db is not None else set())
    return user.is_admin or not repo.allowed_groups or bool(set(repo.allowed_groups) & groups)


def _repo(db: Session, repo_id: str, user: User) -> GitRepository:
    repo = db.get(GitRepository, repo_id)
    if not repo or not _can_deploy_from(repo, user, db):
        raise HTTPException(404, "repository not found")
    return repo


@router.get("/admin/repos", response_model=List[RepoOut], tags=["admin"])
def admin_repos(db: Session = Depends(get_db), secrets: SecretStore = Depends(get_secret_store),
                _: User = Depends(require_admin)):
    return [_repo_out(r, secrets) for r in db.scalars(select(GitRepository).order_by(GitRepository.name))]


@router.put("/admin/repos/{name}", response_model=RepoOut, tags=["admin"])
def upsert_repo(name: str, body: RepoIn, db: Session = Depends(get_db),
                secrets: SecretStore = Depends(get_secret_store), admin: User = Depends(require_admin)):
    if body.name != name:
        raise HTTPException(422, "name in path and body differ")
    try:
        deploying.get_git().check_url(body.url)
    except GitError as exc:
        raise HTTPException(422, str(exc)) from None
    if not valid_branch(body.default_branch):
        raise HTTPException(422, "invalid default branch")
    repo = db.scalar(select(GitRepository).where(GitRepository.name == name)) or GitRepository(name=name)
    if not repo.id:
        from ..ownership import require_create
        repo.owner_group_id = require_create(db,admin).id
    repo.url, repo.default_branch, repo.description, repo.allowed_groups = (
        body.url, body.default_branch, body.description, body.allowed_groups)
    db.add(repo)
    if body.secret is not None:
        secrets.put(repo_path(name), body.secret)
    audit(db, admin.username, "repo.upsert", name, url=body.url, secret_changed=body.secret is not None)
    db.commit()
    return _repo_out(repo, secrets)


@router.delete("/admin/repos/{name}", status_code=204, tags=["admin"])
def delete_repo(name: str, db: Session = Depends(get_db), secrets: SecretStore = Depends(get_secret_store),
                admin: User = Depends(require_admin)):
    repo = db.scalar(select(GitRepository).where(GitRepository.name == name))
    if not repo:
        raise HTTPException(404, "repository not found")
    if db.scalar(select(Workload).where(Workload.repo_id == repo.id, Workload.status != "stopped")):
        raise HTTPException(409, "undeploy the programs deployed from this repository first")
    db.delete(repo)
    secrets.delete(repo_path(name))
    audit(db, admin.username, "repo.delete", name)
    db.commit()


@router.get("/repos", response_model=List[RepoOut])
def list_repos(db: Session = Depends(get_db), secrets: SecretStore = Depends(get_secret_store),
               user: User = Depends(get_actor)):
    """Repositories you may deploy from."""
    repos = db.scalars(select(GitRepository).order_by(GitRepository.name))
    return [_repo_out(r, secrets) for r in repos if _can_deploy_from(r, user, db)]


@router.get("/repos/{repo_id}/branches", response_model=List[BranchOut])
def repo_branches(repo_id: str, db: Session = Depends(get_db), secrets: SecretStore = Depends(get_secret_store),
                  user: User = Depends(get_actor)):
    repo = _repo(db, repo_id, user)
    try:
        heads = deploying.get_git().branches(repo.url, deploying.repo_secret(secrets, repo))
    except GitError as exc:
        raise HTTPException(502, f"cannot read {repo.name}: {exc}") from None
    names = sorted(heads, key=lambda b: (b != repo.default_branch, b))
    return [BranchOut(name=b, sha=heads[b]) for b in names]


@router.post("/repos/{repo_id}/pull", response_model=PullOut)
def repo_pull(repo_id: str, branch: str, db: Session = Depends(get_db),
              secrets: SecretStore = Depends(get_secret_store), user: User = Depends(get_actor)):
    """Fetches a branch and lists the programs in its fdp.json."""
    repo = _repo(db, repo_id, user)
    try:
        commit, programs, errors = deploying.pull(deploying.get_git(), secrets, repo, branch)
    except GitError as exc:
        raise HTTPException(502, f"cannot pull {repo.name}@{branch}: {exc}") from None
    return PullOut(commit=CommitOut(**commit.__dict__), errors=errors,
                   programs={n: p.model_dump(mode="json", by_alias=True) for n, p in programs.items()})


# --- workloads ------------------------------------------------------------------------------

def can_view(pub: Workload, username: str, groups: List[str], is_admin: bool = False, db=None) -> bool:
    db = db or object_session(pub)
    return bool(db and rbac.allowed(db, User(username), "execute", rbac.workload_resource(db, pub)))


def _can_manage(pub: Workload, user: User) -> bool:
    db = object_session(pub)
    return rbac.allowed(db, user, "edit", rbac.workload_resource(db, pub))


def _visible(db: Session, pub_id: str, user: User) -> Workload:
    pub = db.get(Workload, pub_id)
    if not pub or pub.status == "stopped" or not rbac.allowed(db, user, "read", rbac.workload_resource(db, pub)):
        raise HTTPException(404, "workload not found")
    return pub


def pub_out(pub: Workload, user: User, with_events: bool = False) -> WorkloadOut:
    db = object_session(pub)
    resource = rbac.workload_resource(db, pub)
    issue = pub.access_issue if pub.sync_error else None
    # Existing deployments may still have the old, opaque error saved in the database.
    if not issue and pub.sync_error and pub.sync_error.startswith(pub.identity + " is not granted "):
        issue = {"username": pub.identity, "principal_id": None,
                 "sources": pub.sync_error.split(" is not granted ", 1)[1].split(", ")}
    access_issue = data_access_issue(db, User(username=issue["username"], principal_id=issue.get("principal_id")),
                                   issue["sources"], user) if issue else None
    execution_state = "on_demand"
    if pub.kind in ("app", "airflow", "stream"):
        try:
            execution_state = get_orchestrator().status(pub.execution_ref) if pub.execution_ref else "missing"
        except Exception:
            execution_state = "unknown"
    elif pub.kind == "dbt":
        execution_state = "on_demand"
    return WorkloadOut(
        **layers.info(db, resource),
        **owner_info(db,resource),
        execution_state=execution_state,
        resource_id=resource.id, folder_id=resource.parent_id, service_principal_id=pub.service_principal_id,
        execution_mode=pub.execution_mode,
        actions=[a for a in ("read", "execute", "edit", "deploy", "create", "delete", "assign", "identity")
                 if rbac.allowed(object_session(pub), user, a, resource)],
        id=pub.id, slug=pub.slug, title=pub.title, kind=pub.kind, runtime=pub.runtime, program=pub.program,
        repo=pub.repo.name, branch=pub.branch, owner=pub.owner, allowed_groups=pub.allowed_groups,
        warehouse=pub.warehouse, auto_update=pub.auto_update, status=pub.status,
        deployed_sha=pub.deployed_sha, deployed_commit=pub.deployed_commit or {}, deployed_at=pub.deployed_at,
        latest_sha=pub.latest_sha, last_checked_at=pub.last_checked_at,
        queue_position=pub.queue_position, queued_sha=pub.queued_sha,
        sync_error=access_issue["message"] if access_issue else pub.sync_error, access_issue=access_issue,
        program_def=pub.program_def or {}, created_at=pub.created_at, can_manage=_can_manage(pub, user),
        runtime_version=pub.runtime_version, run_as=pub.run_as, identity=pub.identity,
        env=pub.env or {}, secrets=pub.secrets or {},
        url=f"{get_settings().public_url}/p/{pub.slug}/" if pub.kind in ("app", "airflow", "dbt") else None,
        events=[WorkloadEventOut.model_validate(e) for e in pub.events[:50]] if with_events else None,
    )


@router.get("/workloads", response_model=List[WorkloadOut])
def catalog(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Everything deployed that you may use."""
    pubs = list(db.scalars(select(Workload).where(Workload.status != "stopped").order_by(Workload.title)))
    resources = {p.id:rbac.workload_resource(db,p) for p in pubs}
    return [pub_out(p, user) for p in pubs if browsing_allowed(db, user, resources[p.id])]


@router.post("/workloads", response_model=WorkloadOut, status_code=201)
def create_workload(body: DeployIn, db: Session = Depends(get_db), orch: Orchestrator = Depends(get_orchestrator),
            secrets: SecretStore = Depends(get_secret_store), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    parent = db.get(Resource, body.folder_id)
    if not parent or parent.kind not in ("root", "folder"):
        raise HTTPException(422, "choose a workload folder")
    from ..ownership import require_create
    require_create(db,user,parent)
    from ..environments import check_branch
    check_branch(db, parent, body.branch)
    from ..layers import selection
    layer_id = selection(db, body.layer_id, parent)
    if body.allowed_groups:
        raise HTTPException(422, "assign an app group on the folder or service through RBAC")
    repo = _repo(db, body.repo_id, user)
    existing = db.scalar(select(Workload).where(Workload.slug == body.slug))
    if existing and existing.status != "stopped":
        raise HTTPException(409, f"/p/{body.slug} is already taken")
    git = deploying.get_git()
    try:
        commit, programs, errors = deploying.pull(git, secrets, repo, body.branch)
    except GitError as exc:
        raise HTTPException(502, f"cannot pull {repo.name}@{body.branch}: {exc}") from None
    program = programs.get(body.program)
    if program is None:
        raise HTTPException(422, {"message": f"program {body.program!r} not found at {commit.sha[:12]}",
                                  "errors": errors})
    layers.require_program_layer(db, program, layer_id)
    if program.kind == 'stream' and body.execution_mode != 'fixed':
        raise HTTPException(422, 'Continuous streams require a fixed managed identity; there is no interactive caller')
    ident = _identity_for(db, body.run_as, user) if body.run_as else user
    from ..environments import bind
    ident = bind(db, ident, Resource(kind="service", config={"layer_id": layer_id}))
    granted = {d.name for d in granted_sources(db, ident)}
    missing = [d for d in program.data_sources if d not in granted]
    if missing and body.execution_mode == "fixed" and body.run_as and program.kind in ("app", "dbt"):
        raise HTTPException(403, data_access_issue(db, ident, missing, user))
    _check_env(body.env, body.secrets)
    from .. import capacity
    warehouse = capacity.choose_warehouse(db, body.warehouse, program.warehouse, layer_id, program.kind)
    if not warehouse:
        raise HTTPException(422, "choose a warehouse (no default warehouse is configured; an administrator can set one)")
    from .common import require_warehouse
    pool = require_warehouse(db, warehouse, user)  # e.g. a team's dedicated warehouse: only its groups deploy there
    capacity.require_scope(db, pool, layer_id, program.kind)
    worker = getattr(program.airflow_settings, "worker_warehouse", None) if program.kind == "airflow" else None
    if worker:
        capacity.require_scope(db, require_warehouse(db, worker, user), layer_id, program.kind)

    if existing:  # a stopped workload frees its slug
        if existing.resource_id:
            rbac.delete_resource_assignments(db, existing.resource_id)
            db.delete(db.get(Resource, existing.resource_id))
        if existing.service_principal_id:
            p = db.get(DirectoryPrincipal, existing.service_principal_id)
            if p:
                p.active = False
        existing.slug = f"retired-{existing.id}"
        db.flush()
    pub = Workload(
        slug=body.slug, title=body.title or body.program, repo_id=repo.id, branch=body.branch, program=body.program,
        kind=program.kind, runtime=program.runtime or "python", owner=user.username, owner_principal_id=user.principal_id, owner_groups=list(ident.groups),
        execution_mode=body.execution_mode, run_as=body.run_as or None, env=body.env, secrets=body.secrets,
        allowed_groups=body.allowed_groups, warehouse=warehouse, auto_update=body.auto_update,
        status="deploying", latest_sha=commit.sha, program_def=program.model_dump(mode="json", by_alias=True),
    )
    pub.repo = repo
    db.add(pub)
    db.flush()
    resource = Resource(kind="service", name=pub.title, parent_id=parent.id, object_id=pub.id, config={"layer_id": layer_id})
    db.add(resource)
    db.flush()
    pub.resource_id = resource.id
    rbac.own(db,user,resource)
    rbac.workload_resource(db, pub)  # automatically registers the service principal
    deploying.event(db, pub, "created", f"{user.username} deployed {body.program} from {repo.name}@{body.branch}",
                     commit.sha)
    audit(db, user.username, "workload.create", pub.slug, repo=repo.name, branch=body.branch,
          program=body.program, commit=commit.sha)
    db.commit()
    deploying.schedule_deploy(SessionLocal, orch, pub.id, commit.sha, "initial deploy")
    db.refresh(pub)
    return pub_out(pub, user, with_events=True)


@router.get("/workloads/{pub_id}", response_model=WorkloadOut)
def get_workload(pub_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    return pub_out(_visible(db, pub_id, user), user, with_events=True)


@router.patch("/workloads/{pub_id}", response_model=WorkloadOut)
def update_workload(pub_id: str, body: WorkloadPatch, db: Session = Depends(get_db),
                       user: User = Depends(get_actor)):
    pub = _visible(db, pub_id, user)
    if not _can_manage(pub, user):
        raise HTTPException(403, "only the deployer or an admin can change this")
    changes = body.model_dump(exclude_none=True)
    if pub.kind == 'stream' and changes.get('execution_mode', pub.execution_mode) != 'fixed':
        raise HTTPException(422, 'Continuous streams require a fixed managed identity')
    resource = rbac.workload_resource(db, pub)
    if "allowed_groups" in changes:
        raise HTTPException(422, "manage visibility using app-group role assignments")
    if any(k in changes for k in ("run_as", "execution_mode", "secrets", "env")):
        rbac.require(db, user, "identity", resource)
    if "run_as" in changes:
        _identity_for(db, changes["run_as"] or None, user)
        if changes["run_as"]:
            from ..environments import bind
            bind(db, _identity_for(db, changes["run_as"], user), resource)
        changes["run_as"] = changes["run_as"] or None
    _check_env(changes.get("env", {}), changes.get("secrets", {}))
    if "warehouse" in changes:
        from .common import require_warehouse
        from .. import capacity
        from ..layers import info
        pool = require_warehouse(db, changes["warehouse"], user)
        capacity.require_scope(db, pool, info(db, resource)["layer_id"], pub.kind)
    for field, value in changes.items():
        setattr(pub, field, value)
    redeploy = any(k in changes for k in ("run_as", "execution_mode", "env", "secrets", "warehouse"))
    deploying.event(db, pub, "settings_changed", f"{user.username} changed {', '.join(changes)}"
                     + (" — redeploying" if redeploy and pub.deployed_sha else ""))
    db.commit()
    if redeploy and pub.deployed_sha and pub.kind in ("app", "airflow", "dbt", "stream"):
        deploying.schedule_deploy(SessionLocal, get_orchestrator(), pub.id, pub.deployed_sha, "settings changed")
    return pub_out(pub, user, with_events=True)


def _identity_for(db: Session, run_as: Optional[str], user: User) -> User:
    """You, or a service account you may use."""
    if not run_as:
        return user
    sa = get_sa(db, run_as)
    if sa is None or not may_use(sa, user):
        raise HTTPException(403, f"you may not run as service account {run_as!r}")
    return rbac.resolve_user(db, as_user(sa))


def _check_env(env: dict, secrets: dict) -> None:
    from ..programs import Program

    try:  # same rules as fdp.json
        Program.model_validate({"entrypoint": "x.py", "env": env, "secrets": secrets})
    except Exception as exc:
        raise HTTPException(422, f"environment: {exc}") from None


@router.post("/workloads/{pub_id}/sync", response_model=WorkloadOut)
def sync_now(pub_id: str, db: Session = Depends(get_db), orch: Orchestrator = Depends(get_orchestrator),
             secrets: SecretStore = Depends(get_secret_store), user: User = Depends(get_actor)):
    """Check the branch for new commits right away (the GitOps loop also does this periodically)."""
    pub = _visible(db, pub_id, user)
    rbac.require(db, user, "deploy", rbac.workload_resource(db, pub))
    pub.failed_sha = None  # an explicit check retries a commit that failed before
    target = deploying.check(db, orch, secrets, pub)
    if target:
        deploying.schedule_deploy(SessionLocal, orch, pub.id, *target)
    db.refresh(pub)
    return pub_out(pub, user, with_events=True)


@router.delete("/workloads/{pub_id}", status_code=204)
def undeploy(pub_id: str, db: Session = Depends(get_db), orch: Orchestrator = Depends(get_orchestrator),
              user: User = Depends(get_actor)):
    pub = _visible(db, pub_id, user)
    rbac.require(db, user, "delete", rbac.workload_resource(db, pub))
    for ref in {pub.execution_ref, *(pub.execution_refs or [])}:
        if ref:
            orch.stop(ref, grace_seconds=5)
    pub.status, pub.execution_ref, pub.upstream_url, pub.execution_refs = "stopped", None, None, []
    pub.queued_sha = pub.queued_at = pub.queued_demand = None
    if pub.service_principal_id:
        db.get(DirectoryPrincipal, pub.service_principal_id).active = False
    deploying.event(db, pub, "undeployed", f"{user.username} undeployed it")
    audit(db, user.username, "workload.delete", pub.slug)
    db.commit()


@router.post("/workloads/{pub_id}/open", response_model=OpenOut)
def open_workload(pub_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    pub = _visible(db, pub_id, user)
    rbac.require(db, user, "execute", rbac.workload_resource(db, pub))
    if pub.kind not in ("app", "airflow", "dbt") or pub.status != "live":
        raise HTTPException(409, "only live apps, Airflow deployments and dbt docs can be opened")
    ticket = mint_token("ticket", user.username, 60, target=f"p:{pub.slug}", groups=user.groups, pid=user.principal_id)
    return OpenOut(url=f"{get_settings().public_url}/p/{pub.slug}/?dc_ticket={ticket}")


@router.get("/workloads/{pub_id}/orchestrator-runs")
def orchestrator_runs(pub_id: str, limit: int = Query(5, ge=1, le=25), dag_id: str = Query("", max_length=250),
                      db: Session = Depends(get_db), user: User = Depends(get_actor),
                      store: SecretStore = Depends(get_secret_store)):
    pub = _visible(db, pub_id, user)
    rbac.require(db, user, "execute", rbac.workload_resource(db, pub))
    if pub.kind != "airflow":
        raise HTTPException(409, "this workload is not an orchestrator")
    from ..airflow_history import recent_runs
    try:
        from ..artifacts import for_workload
        return recent_runs(pub, store, limit, dag_id, artifacts=for_workload(db, pub))
    except Exception:
        # Never return driver messages, connection strings, or deployment credentials.
        raise HTTPException(503, "Orchestrator run history is temporarily unavailable. Open the deployment for details.") from None


@router.post("/workloads/{pub_id}/runs", response_model=RunOut, status_code=201)
def run_deployed(pub_id: str, body: DeployedRunIn, db: Session = Depends(get_db),
                  orch: Orchestrator = Depends(get_orchestrator), user: User = Depends(get_actor)):
    """Run a deployed program with your own data access, on the deployed commit."""
    pub = _visible(db, pub_id, user)
    return run_out(_start_deployed(db, orch, pub, user, body.args, body.pool))


def _start_deployed(db: Session, orch: Orchestrator, pub: Workload, user: User, args: dict,
                     pool: Optional[str]):
    if pub.kind == "stream":
        raise HTTPException(409, "Continuous streams run as deployments; use deployment controls to update or stop them")
    if pub.kind in ("app", "airflow"):
        raise HTTPException(409, f"{pub.kind} workloads are opened, not run")
    if pub.status != "live" or not pub.deployed_sha:
        raise HTTPException(409, "this program is not live yet")
    from ..programs import Program
    from ..executions import with_workload_settings

    rbac.require(db, user, "execute", rbac.workload_resource(db, pub))
    viewer = user
    initiator, initiator_id = user.username, user.principal_id
    if pub.execution_mode == "fixed":
        user = deploying.identity_of(db, pub)
        if user is None:
            raise HTTPException(403, "execution identity is disabled")
    from ..environments import bind
    user = bind(db, user, rbac.workload_resource(db, pub))
    granted = {d.name for d in granted_sources(db, user)}
    plan = checked_plan(pub.program, Program.model_validate(pub.program_def), args, granted,
                        db=db, actor=user, viewer=viewer)
    with_workload_settings(plan, pub.env or {}, pub.secrets or {})
    plan.workload_id, plan.commit_sha = pub.id, pub.deployed_sha
    from .. import capacity
    return start_run(db, orch, user, plan, capacity.choose_warehouse(db, pool, pub.warehouse, user.environment_id, plan.kind),
                     None, pub.program, initiated_by=initiator, initiator_id=initiator_id)


@router.post("/orchestrate/runs", response_model=RunOut, status_code=201)
def orchestrate(body: OrchestrateIn, db: Session = Depends(get_db), orch: Orchestrator = Depends(get_orchestrator),
                wl: Execution = Depends(get_execution)):
    """Used by Airflow's FDPRunOperator (and service tokens) to start runs.

    workload=<name>: a deployed program, run as the caller.
    program=<name>: from an Airflow deployment, a program in the same repo at its deployed commit;
                    from a workspace (test deployment), a program in that workspace.
    """
    from ..programs import Program
    from ..executions import with_workload_settings

    from ..environments import execution_user
    caller = rbac.resolve_user(db, execution_user(db, wl))
    if body.workload:
        pub = db.scalar(select(Workload).where(Workload.slug == body.workload, Workload.status != "stopped"))
        if pub is None or not rbac.allowed(db, caller, "execute", rbac.workload_resource(db, pub)):
            raise HTTPException(404, f"deployed program {body.workload!r} not found")
        return run_out(_start_deployed(db, orch, pub, caller, body.args, body.warehouse))

    granted = {d.name for d in granted_sources(db, caller)}
    if wl.workload_id:
        pub = db.get(Workload, wl.workload_id)
        if pub is None or pub.status == "stopped" or not pub.deployed_sha:
            raise HTTPException(409, "the calling deployment is not live")
        programs, errors = deploying.programs_at(deploying.get_git(), pub.repo, pub.deployed_sha)
        program = programs.get(body.program or "")
        if program is None:
            raise HTTPException(404, f"program {body.program!r} is not in {pub.repo.name} at {pub.deployed_sha[:12]}"
                                     + (f" ({'; '.join(errors)})" if errors else ""))
        if program.kind in ("app", "airflow", "stream"):
            raise HTTPException(409, f"{program.kind} programs cannot be run as a task")
        plan = checked_plan(body.program, program, body.args, granted, db=db, actor=caller)
        # Same commit and production settings as the Airflow deployment itself.
        with_workload_settings(plan, pub.env or {}, pub.secrets or {})
        plan.workload_id, plan.commit_sha = pub.id, pub.deployed_sha
        from .. import capacity
        from ..environments import bind
        layer_id = bind(db, caller, rbac.workload_resource(db, pub)).environment_id
        return run_out(start_run(db, orch, caller, plan,
                                 capacity.choose_warehouse(db, body.warehouse, program.warehouse, layer_id, plan.kind), None, body.program))

    if wl.workspace_id:  # a test deployment running from a workspace
        from ..models import Workspace
        from .runs import RunIn, _plan

        ws = db.get(Workspace, wl.workspace_id)
        if ws is None or not rbac.same_identity(ws, caller):
            raise HTTPException(404, "workspace not found")
        req = RunIn(workspace_id=ws.id, program=body.program, args=body.args, pool=body.warehouse)
        plan, pool_name = _plan(req, db, orch, ws, caller, granted)
        if plan.kind in ("app", "airflow", "stream"):
            raise HTTPException(409, f"{plan.kind} programs cannot be run as a task")
        return run_out(start_run(db, orch, caller, plan, pool_name, ws.id, body.program))
    raise HTTPException(422, "use 'workload' with a service token")


@router.get("/workloads/{pub_id}/source", include_in_schema=False)
def source(pub_id: str, sha: str, db: Session = Depends(get_db), wl: Execution = Depends(get_execution),
           secrets: SecretStore = Depends(get_secret_store)):
    """Source archive of a deployed commit, for that workload's own executions only."""
    pub = db.get(Workload, pub_id)
    if not pub or wl.workload_id != pub_id:
        raise HTTPException(404)
    # Only commits this workload has been at (not arbitrary history of the repository).
    if not wl.commit_sha or sha != wl.commit_sha:
        raise HTTPException(404)
    git = deploying.get_git()
    try:
        data = git.archive(pub.repo_id, sha)
    except GitError:
        # The cache is rebuilt on demand (it is empty after a control-plane restart): fetch the
        # workload's branch once, which brings the deployed commit's history, then retry.
        repo = db.get(GitRepository, pub.repo_id)
        try:
            if repo is None:
                raise GitError("repository no longer registered")
            git.fetch(repo.id, repo.url, pub.branch, deploying.repo_secret(secrets, repo))
            data = git.archive(pub.repo_id, sha)
        except GitError as exc:
            raise HTTPException(404, str(exc)) from None
    return Response(content=data, media_type="application/gzip")
