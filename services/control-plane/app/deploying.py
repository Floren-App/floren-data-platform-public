"""Deploying and GitOps: deployed programs follow their branch.

Desired state = the head of the workload's branch. A reconcile loop compares it with what is
deployed and converges:
  * new commit with a valid fdp.json  -> deploy it (apps: start new version, wait until it
    answers, switch traffic, stop the old one — zero downtime)
  * new commit that is broken               -> keep the current version, record why, don't retry it
  * app container gone or token aging       -> restart the deployed commit (self-healing)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set, Tuple

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import User
from .access_errors import data_access_issue
from .config import Settings, get_settings
from .deps import get_secret_store, granted_sources
from .identity import SecretAccessError, as_user, get_sa, resolve_secret_env
from .git import Commit, GitClient, GitError
from .orchestrator.base import CapacityError
from .models import ComputePool, GitRepository, Workload, WorkloadEvent, utcnow
from .orchestrator import Orchestrator
from .programs import CONFIG_FILE, Program, merge_configs
from .runtimes import RuntimeUnavailable
from .runtimes import resolve as resolve_runtime
from .secrets import SecretStore, repo_path
from .executions import deployed_service_spec

log = logging.getLogger("fdp.gitops")

TOKEN_REFRESH = timedelta(days=20)   # app tokens live 30 days; redeploy before that
SWITCH_DELAY_SECONDS = 4             # let the gateway pick up the new route before stopping the old

_git: Optional[GitClient] = None
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deploy")
_deploying: Set[str] = set()
_lock = threading.Lock()
# Tests run deploys inline and replace the readiness probe.
RUN_INLINE = False


def get_git() -> GitClient:
    global _git
    if _git is None:
        s = get_settings()
        _git = GitClient(s.git_cache_dir, allow_file_urls=s.allow_file_repos)
    return _git


def http_ready(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except httpx.HTTPError:
        return False


probe: Callable[[str], bool] = http_ready


class DeployError(Exception):
    pass


def repo_secret(secrets: SecretStore, repo: GitRepository) -> Dict[str, str]:
    return secrets.read(repo_path(repo.name))


def programs_at(git: GitClient, repo: GitRepository, sha: str) -> Tuple[Dict[str, Program], List[str]]:
    """Programs in the repository's root fdp.json at a commit (must already be fetched)."""
    text = git.read_file(repo.id, sha, CONFIG_FILE)
    if text is None:
        return {}, [f"{CONFIG_FILE} not found at the repository root"]
    resolved, _warehouse, errors = merge_configs({"": text})
    return {name: r.program for name, r in resolved.items()}, errors


def pull(git: GitClient, secrets: SecretStore, repo: GitRepository, branch: str) -> Tuple[Commit, Dict[str, Program], List[str]]:
    """Fetches a branch and reads its fdp.json: what the Deploy tab shows before deploying."""
    sha = git.fetch(repo.id, repo.url, branch, repo_secret(secrets, repo))
    programs, errors = programs_at(git, repo, sha)
    return git.commit(repo.id, sha), programs, errors


def event(db: Session, pub: Workload, kind: str, message: str, sha: Optional[str] = None) -> None:
    db.add(WorkloadEvent(workload_id=pub.id, kind=kind, message=message, sha=sha))
    log.info("workload %s: %s %s %s", pub.slug, kind, (sha or "")[:12], message)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


def _ready_path(pub: Workload) -> str:
    """Where a new version must answer before it goes live."""
    if pub.kind == "airflow":
        return f"/p/{pub.slug}/health"
    # Streamlit serves under its base URL path; Shiny is served at / behind a stripped prefix.
    return f"/p/{pub.slug}/" if pub.runtime == "python" else "/"


def _stop_components(orch: Orchestrator, pub: Workload, keep_broker: bool, except_ref: Optional[str] = None) -> None:
    """Stops the extra containers of a multi-container deployment (not execution_ref itself)."""
    for ref in pub.execution_refs or []:
        if ref in (except_ref, pub.execution_ref) or (keep_broker and ref.endswith(("-broker", "-metadata"))):
            continue
        orch.stop(ref, grace_seconds=10)


def _shared_airflow_database(db, orch, pub, store):
    """Never silently abandon an existing deployment's orchestration history."""
    from .airflow_db import AirflowDatabaseError, ensure_database, keys_path
    if get_settings().airflow_metadata_url and pub.deployed_sha and (store.read(keys_path(pub.id)) or {}).get('metadata_backend') != 'shared-postgres':
        from .airflow_history import recent_runs
        from .artifacts import for_workload
        try:
            history = recent_runs(pub, store, limit=1, artifacts=for_workload(db, pub, orch))
        except Exception:
            raise AirflowDatabaseError("cannot check existing Airflow history; migrate or verify its metadata before switching to shared PostgreSQL") from None
        if history['runs']:
            raise AirflowDatabaseError("existing Airflow history must be migrated before switching to shared PostgreSQL")
    return ensure_database(store, pub.id)


def _deploy_distributed_airflow(db: Session, orch: Orchestrator, pub: Workload, ident: User, sha: str,
                                program: Program, pool: ComputePool, image: str,
                                secret_env: Dict[str, str], layer: bool = False) -> Tuple[bool, str]:
    """Recreate: old components stop first (never two schedulers); broker and metadata stay up across deploys.

    In a managed layer every component runs in the layer namespace, with the deployment's own metadata
    database there too (data on the layer's artifact volume) and a password-protected broker."""
    from .airflow_db import AirflowDatabaseError, deployment_keys, layer_database_url
    from .artifacts import place
    from .executions import airflow_broker_spec, airflow_distributed_specs, airflow_metadata_spec

    s = get_settings()
    cfg = program.airflow_settings
    worker_pool = pool
    if cfg.worker_warehouse:
        worker_pool = db.scalar(select(ComputePool).where(ComputePool.name == cfg.worker_warehouse))
        if worker_pool is None:
            return False, f"worker warehouse {cfg.worker_warehouse!r} does not exist"
        from .capacity import trino_warehouses
        if worker_pool.name in trino_warehouses(db):
            return False, f"worker warehouse {worker_pool.name!r} runs Trino workers; only Trino uses it"
    store = get_secret_store()
    keys = deployment_keys(store, pub.id)
    if layer and not s.airflow_metadata_url:
        db_url = layer_database_url(pub.id, keys)
    else:
        try:
            db_url = _shared_airflow_database(db, orch, pub, store)
        except AirflowDatabaseError as exc:
            return False, str(exc)
    specs = airflow_distributed_specs(s, pub.id, pub.slug, sha, program, pool, worker_pool, image, ident.username,
                                      ident.groups, granted_sources(db, ident), db_url, keys,
                                      extra_env=pub.env or {}, secret_env=secret_env,
                                      broker_password=keys["broker_password"] if layer else None)
    broker = airflow_broker_spec(s, pub.id, keys["broker_password"] if layer else None)
    backends = [broker, *([airflow_metadata_spec(s, pub.id, keys["password"])] if layer and not s.airflow_metadata_url else [])]
    for spec in [*backends, *specs]:
        if ident.environment_id:
            spec.labels["fdp.floren.app/environment"] = ident.environment_id
            spec.labels["fdp.floren.app/layer"] = ident.environment_id
        if layer:
            try:
                place(db, spec, ident.environment_id, orch)
            except HTTPException as exc:
                return False, placement_problem(exc)

    def ref(spec):
        return f"{spec.placement.namespace}/{spec.name}" if spec.placement else spec.name
    kept = {ref(b) for b in backends}
    # Stop everything of the previous version except the broker and metadata database.
    for old in [pub.execution_ref, *(pub.execution_refs or [])]:
        if old and old not in kept:
            orch.stop(old, grace_seconds=10)
    pub.execution_ref = pub.upstream_url = None
    try:
        for backend in backends:
            if orch.status(ref(backend)) != "running":
                orch.start(backend)
        if layer and not s.airflow_metadata_url:
            ok, why = _wait_metadata(orch, ref(backends[1]), s.app_start_timeout_seconds)
            if not ok:
                return False, why
        handles = [orch.start(spec) for spec in specs]
    except CapacityError as exc:
        for spec in specs:
            orch.stop(ref(spec))
        pub.execution_refs = sorted(kept)
        return False, str(exc)
    pub.execution_refs = [*(ref(b) for b in backends), *(h.ref for h in handles)]
    db.commit()
    web = handles[0]
    ok, why = _wait_ready(orch, web.ref, f"{web.upstream_url}{_ready_path(pub)}", s.app_start_timeout_seconds)
    if not ok:
        tail = orch.logs(web.ref, tail=15).strip()
        for h in handles:
            orch.stop(h.ref)
        pub.execution_refs = [ref(b) for b in backends]
        return False, why + (f"\n{tail}" if tail else "")
    pub.execution_ref, pub.upstream_url = web.ref, web.upstream_url
    return True, ""


def _wait_metadata(orch: Orchestrator, ref: str, timeout: float) -> Tuple[bool, str]:
    """Scheduler and workers do not restart on their own, so the database must accept logins first."""
    check = getattr(orch, "execution_exec", None)
    if check is None:
        return True, ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, _ = check(ref, ["pg_isready", "-q", "-h", "127.0.0.1", "-U", "airflow", "-d", "airflow"])
        if code == 0:
            return True, ""
        if orch.status(ref) in ("failed", "succeeded"):
            break
        time.sleep(0 if RUN_INLINE else 3)
    tail = orch.logs(ref, tail=15).strip()
    return False, "the Airflow metadata database did not start" + (f"\n{tail}" if tail else "")


def placement_problem(exc: HTTPException) -> str:
    """A readable reason when a deployment cannot be placed in its layer."""
    detail = exc.detail
    return detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)


def _managed_layer(db: Session, layer_id) -> bool:
    from .layer_infrastructure import placement
    return placement(db, layer_id) is not None


def docs_path(pub_id: str) -> str:
    return os.path.join(get_settings().data_dir, docs_artifact(pub_id))


def docs_artifact(pub_id: str) -> str:
    return f"dbt-docs/{pub_id}/index.html"


def _dbt_docs_job(db: Session, orch: Orchestrator, pub: Workload, ident: User, sha: str, program: Program,
                  pool: ComputePool, image: str, secret_env: Dict[str, str]) -> Tuple[bool, str]:
    """A dbt version goes live once its docs (with lineage) generate against production.

    Runs as a normal run of the workload's identity (visible in its runs) with command=docs.
    """
    from .models import Run
    from .executions import plan_from_program, run_spec, with_workload_settings

    s = get_settings()
    plan = with_workload_settings(plan_from_program(program, {"command": "docs"}, set(program.data_sources)),
                                     pub.env or {}, {})
    plan.workload_id, plan.commit_sha, plan.image, plan.secret_env = pub.id, sha, image, secret_env
    plan.label = f"docs for deployed dbt project {pub.slug} at {sha[:12]}"
    from .data_access import snapshot
    from .ownership import creation_data_snapshot
    run = Run(owner=ident.username, principal_id=ident.principal_id, initiator_id=pub.owner_principal_id, initiated_by=pub.owner, access_snapshot=snapshot(db, ident), environment_id=ident.environment_id or None, workload_id=pub.id, commit_sha=sha, pool_id=pool.id, kind="dbt",
              primary_create_snapshot=creation_data_snapshot(db,ident),
              primary_group_id=ident.active_group_id or None,
              entrypoint=program.entrypoint, program=pub.program, args=plan.args, runtime="dbt",
              runtime_version=plan.version)
    run.pool = pool
    db.add(run)
    db.flush()
    from .artifacts import for_layer, place
    store = for_layer(db, ident.environment_id, orch)
    before = store.mtime(docs_artifact(pub.id))
    db.commit()
    spec = run_spec(s, run, pool, plan, ident.groups, granted_sources(db, ident))
    try:
        place(db, spec, ident.environment_id, orch)
    except HTTPException as exc:
        run.status, run.finished_at = "failed", utcnow()
        db.commit()
        return False, placement_problem(exc)
    try:
        handle = orch.start(spec)
    except CapacityError as exc:
        run.status, run.finished_at = "failed", utcnow()
        db.commit()
        return False, str(exc)
    run.execution_ref, run.status = handle.ref, "running"
    db.commit()
    deadline = time.monotonic() + s.app_start_timeout_seconds
    state = "running"
    while time.monotonic() < deadline:
        state = orch.status(handle.ref)
        if state in ("succeeded", "failed", "missing"):
            break
        time.sleep(2)
    fresh = store.mtime(docs_artifact(pub.id)) > before
    run.status, run.finished_at = ("succeeded" if state == "succeeded" else "failed"), utcnow()
    db.commit()
    if state != "succeeded" or not fresh:
        tail = orch.logs(handle.ref, tail=15).strip()
        return False, f"dbt docs could not be generated ({state})" + (f"\n{tail}" if tail else "")
    return True, ""


def _wait_ready(orch: Orchestrator, ref: str, url: str, timeout: int) -> Tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = orch.status(ref)
        if state in ("failed", "succeeded", "missing"):
            return False, f"the app exited during start-up ({state})"
        if probe(url):
            return True, ""
        time.sleep(2)
    return False, f"the app did not answer within {timeout} s"


def identity_of(db: Session, pub: Workload) -> Optional[User]:
    """Who the workload's executions act as. A service account's groups are read fresh, so
    access changes apply at the next deploy. None if its service account is gone or disabled."""
    from .rbac import workload_resource, resolve_user
    workload_resource(db, pub)
    if pub.run_as:
        sa = get_sa(db, pub.run_as)
        if not sa or sa.disabled:
            return None
        from .environments import bind
        return bind(db, resolve_user(db, as_user(sa)), workload_resource(db, pub))
    # Persistent infrastructure never runs as a human deployer, even in caller mode.
    return resolve_user(db, User(username=f"service:{pub.service_principal_id}"))


def deploy(db: Session, orch: Orchestrator, git: GitClient, pub: Workload, sha: str, reason: str) -> bool:
    """Makes `sha` the live version. Returns False (keeping the current version) when it can't."""
    s = get_settings()
    repo = pub.repo
    try:
        try:
            commit = git.commit(repo.id, sha)
        except GitError:
            # Settings changes can land on a replica whose ephemeral Git cache is empty.
            # Fetch the branch, then read the requested commit; never promote its new head.
            git.fetch(repo.id, repo.url, pub.branch, repo_secret(get_secret_store(), repo))
            commit = git.commit(repo.id, sha)
    except GitError:
        return _failed(db, pub, sha, "cannot fetch the pinned source revision; verify repository access and commit availability")
    programs, errors = programs_at(git, repo, sha)
    program = programs.get(pub.program)
    ident = identity_of(db, pub)
    if ident is None:
        return _failed(db, pub, sha, f"service account {pub.run_as!r} is missing or disabled")
    pub.owner_groups = []  # no deployer authorization snapshots
    problem = None
    if program is None:
        problem = "; ".join(errors) or f"program {pub.program!r} is no longer in {CONFIG_FILE}"
    elif program.kind != pub.kind:
        problem = f"program {pub.program!r} changed from {pub.kind} to {program.kind}; deploy it again"
    if not problem:
        from .layers import require_program_layer, resource_layer
        from .rbac import workload_resource
        try:
            require_program_layer(db, program, resource_layer(db, workload_resource(db, pub)))
        except HTTPException as exc:
            problem = str(exc.detail)
    if not problem and pub.kind in ("app", "dbt") and pub.execution_mode == "fixed":
        granted = {d.name for d in granted_sources(db, ident)}
        missing = [d for d in program.data_sources if d not in granted]
        if missing:
            issue = data_access_issue(db, ident, missing)
            return _failed(db, pub, sha, issue["message"],
                           {"username": ident.username, "principal_id": ident.principal_id, "sources": missing})
    if problem:
        return _failed(db, pub, sha, problem)

    try:
        rt = resolve_runtime(db, program.image_kind, program.version)
    except RuntimeUnavailable as exc:
        return _failed(db, pub, sha, str(exc))
    try:
        secret_env, _paths = resolve_secret_env(db, get_secret_store(), ident, {**program.secrets, **(pub.secrets or {})})
    except SecretAccessError as exc:
        return _failed(db, pub, sha, f"secrets: {exc}")
    old_ref = None
    if pub.kind in ("app", "airflow", "dbt"):
        pool = db.scalar(select(ComputePool).where(ComputePool.name == pub.warehouse))
        if pool is None:
            return _failed(db, pub, sha, f"warehouse {pub.warehouse!r} no longer exists")
        from . import capacity
        need = capacity.demand(pub, program)
        more = capacity.extra(need, pub)
        if more:
            # Deploys and run admissions to these warehouses are serialized while they count.
            list(db.scalars(select(ComputePool).where(ComputePool.name.in_(list(more))).with_for_update()))
            blocked = capacity.shortfall(db, need, replacing=pub)
            behind = pub.queued_sha is None and next((name for name in more if capacity.queue(db, name)), None)
            if blocked or behind:
                return _queued(db, pub, sha, need, blocked or behind)
        pub.queued_sha = pub.queued_at = pub.queued_demand = None
        if pub.status != "live":
            pub.status = "deploying"
        db.commit()
    if pub.kind == "dbt":
        ok, why = _dbt_docs_job(db, orch, pub, ident, sha, program, pool, rt.image, secret_env)
        if not ok:
            return _failed(db, pub, sha, why)
    elif pub.kind == "airflow" and program.airflow_settings and program.airflow_settings.mode == "distributed":
        ok, why = _deploy_distributed_airflow(db, orch, pub, ident, sha, program, pool, rt.image, secret_env,
                                              layer=_managed_layer(db, ident.environment_id))
        if not ok:
            return _failed(db, pub, sha, why)
    elif pub.kind in ("app", "airflow"):
        spec = deployed_service_spec(s, pub.id, pub.slug, sha, program, pool, rt.image, ident.username,
                                      ident.groups, granted_sources(db, ident),
                                      extra_env=pub.env or {}, secret_env=secret_env)
        if pub.kind == "airflow":
            spec.labels["fdp.floren.app/airflow-role"] = "standalone"
            if s.airflow_metadata_url:
                from .airflow_db import AirflowDatabaseError, deployment_keys
                store = get_secret_store()
                try:
                    connection = _shared_airflow_database(db, orch, pub, store)
                except AirflowDatabaseError as exc:
                    return _failed(db, pub, sha, str(exc))
                keys = deployment_keys(store, pub.id)
                spec.secret_env.update({"AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": connection,
                    "AIRFLOW__CORE__FERNET_KEY": keys["fernet_key"],
                    "AIRFLOW__WEBSERVER__SECRET_KEY": keys["secret_key"]})
                spec.env.update({"AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_SIZE": "2",
                    "AIRFLOW__DATABASE__SQL_ALCHEMY_MAX_OVERFLOW": "1"})
        if ident.environment_id:
            spec.labels["fdp.floren.app/environment"] = ident.environment_id
            spec.labels["fdp.floren.app/layer"] = ident.environment_id
        if pub.execution_mode == "caller":
            spec.env["FDP_EXECUTION_MODE"] = "caller"
        from .artifacts import place
        try:
            place(db, spec, ident.environment_id, orch)
        except HTTPException as exc:
            return _failed(db, pub, sha, placement_problem(exc))
        _stop_components(orch, pub, keep_broker=False)  # only after metadata/placement validation
        old_ref = pub.execution_ref
        next_ref = f"{spec.placement.namespace}/{spec.name}" if spec.placement else spec.name
        if old_ref == next_ref:      # same commit (restart): nothing to switch from
            old_ref = None
        if pub.kind == "airflow" and old_ref:
            # Recreate, not blue/green: two schedulers on one metadata DB would run DAGs twice.
            orch.stop(old_ref, grace_seconds=10)
            old_ref = None
        try:
            handle = orch.start(spec)
        except CapacityError as exc:
            return _failed(db, pub, sha, str(exc))
        ok, why = _wait_ready(orch, handle.ref, f"{handle.upstream_url}{_ready_path(pub)}", s.app_start_timeout_seconds)
        if not ok:
            tail = orch.logs(handle.ref, tail=15).strip()
            orch.stop(handle.ref)
            if pub.kind == "airflow":
                pub.execution_ref = pub.upstream_url = None
            return _failed(db, pub, sha, why + (f"\n{tail}" if tail else ""))
        pub.execution_ref, pub.upstream_url = handle.ref, handle.upstream_url
        pub.execution_refs = [handle.ref]
    pub.runtime_version = rt.version
    pub.deployed_sha, pub.deployed_at, pub.status = sha, utcnow(), "live"
    pub.deployed_commit = {"subject": commit.subject, "author": commit.author, "date": commit.date}
    pub.program_def = program.model_dump(mode="json", by_alias=True)
    pub.runtime = program.runtime or pub.runtime
    pub.sync_error, pub.failed_sha = None, None
    pub.access_issue = None
    event(db, pub, "deployed", f"{reason}: {commit.subject} ({commit.author})", sha)
    db.commit()
    if old_ref:
        time.sleep(SWITCH_DELAY_SECONDS if not RUN_INLINE else 0)
        orch.stop(old_ref, grace_seconds=5)
    return True


def _queued(db: Session, pub: Workload, sha: str, need: Dict[str, int], warehouse: str) -> bool:
    """Not enough room on a warehouse: wait in line (the dispatcher deploys it); keep serving any live version."""
    first = pub.queued_sha != sha
    pub.queued_sha, pub.queued_demand = sha, need
    pub.queued_at = pub.queued_at or utcnow()
    if not pub.deployed_sha:
        pub.status = "queued"
    pub.sync_error = f"waiting for capacity on warehouse {warehouse}"
    if first:
        event(db, pub, "queued", f"waiting for capacity on warehouse {warehouse}"
              + (f" — still serving {pub.deployed_sha[:12]}" if pub.deployed_sha else ""), sha)
    db.commit()
    return False


def _failed(db: Session, pub: Workload, sha: str, problem: str, access_issue: Optional[dict] = None) -> bool:
    pub.failed_sha, pub.sync_error = sha, problem
    pub.access_issue = access_issue
    if not pub.deployed_sha:
        pub.status = "failed"
    event(db, pub, "deploy_failed", problem + ("" if not pub.deployed_sha else
                                                f" — still serving {pub.deployed_sha[:12]}"), sha)
    db.commit()
    return False


def schedule_deploy(session_factory, orch: Orchestrator, pub_id: str, sha: str, reason: str) -> bool:
    """Runs a deploy in the background (one at a time per workload)."""
    with _lock:
        if pub_id in _deploying:
            return False
        _deploying.add(pub_id)

    def work() -> None:
        try:
            from .db import engine
            from .leadership import cluster_lock
            # One deploy per workload across all control plane replicas, not only within this one.
            with cluster_lock(engine, f"deploy:{pub_id}") as mine:
                if not mine:
                    return
                with session_factory() as db:
                    pub = db.get(Workload, pub_id)
                    if pub and pub.status != "stopped":
                        deploy(db, orch, get_git(), pub, sha, reason)
        except Exception:
            log.exception("deploy of %s failed", pub_id)
        finally:
            with _lock:
                _deploying.discard(pub_id)

    if RUN_INLINE:
        work()
    else:
        _executor.submit(work)
    return True


def check(db: Session, orch: Orchestrator, secrets: SecretStore, pub: Workload) -> Optional[Tuple[str, str]]:
    """Compares the branch head with what's deployed. Returns (sha, reason) when a deploy is needed."""
    git = get_git()
    pub.last_checked_at = utcnow()
    try:
        head = git.fetch(pub.repo.id, pub.repo.url, pub.branch, repo_secret(secrets, pub.repo))
    except GitError as exc:
        msg = f"cannot read {pub.repo.name}@{pub.branch}: {exc}"
        if pub.sync_error != msg:
            pub.sync_error = msg
            pub.access_issue = None
            event(db, pub, "sync_failed", msg)
        db.commit()
        return None
    pub.latest_sha = head
    if pub.sync_error and pub.sync_error.startswith("cannot read"):
        pub.sync_error = None
    if pub.queued_sha:
        # Waiting for capacity: the dispatcher deploys it. A newer commit replaces the queued one.
        if pub.auto_update and head not in (pub.queued_sha, pub.deployed_sha, pub.failed_sha):
            pub.queued_sha = head
        db.commit()
        return None
    db.commit()

    if pub.auto_update and head != pub.deployed_sha and head != pub.failed_sha:
        event(db, pub, "update_found", f"new commit on {pub.branch}: {git.commit(pub.repo.id, head).subject}", head)
        db.commit()
        return head, "update"
    if pub.kind in ("app", "airflow") and pub.deployed_sha:
        refs = [r for r in {pub.execution_ref, *(pub.execution_refs or [])} if r] or [None]
        down = [r or "container" for r in refs if (orch.status(r) if r else "missing") != "running"]
        if down:
            event(db, pub, "restarting", f"not running: {', '.join(sorted(down))}", pub.deployed_sha)
            db.commit()
            return pub.deployed_sha, "restart"
        if pub.deployed_at and utcnow() - _aware(pub.deployed_at) > TOKEN_REFRESH:
            return pub.deployed_sha, "periodic refresh"
    return None


def reconcile_once(session_factory, orch: Orchestrator, secrets: SecretStore) -> None:
    with session_factory() as db:
        pubs = db.scalars(select(Workload).where(Workload.status != "stopped")).all()
        todo = []
        for pub in pubs:
            if pub.id in _deploying:
                continue
            try:
                target = check(db, orch, secrets, pub)
            except Exception:
                log.exception("checking %s failed", pub.slug)
                continue
            if target:
                todo.append((pub.id, *target))
    for pub_id, sha, reason in todo:
        schedule_deploy(session_factory, orch, pub_id, sha, reason)


async def gitops_loop(session_factory, orch: Orchestrator, secrets: SecretStore, s: Settings) -> None:
    while True:
        await asyncio.sleep(s.gitops_interval_seconds)
        try:
            await asyncio.to_thread(reconcile_once, session_factory, orch, secrets)
        except Exception:  # keep syncing; one bad tick must not stop GitOps
            log.exception("gitops tick failed")
