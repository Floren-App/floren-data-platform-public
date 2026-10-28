"""Warehouse capacity and the queue for work that does not fit yet.

A warehouse's `max_concurrent` bounds the containers running on it at once: jobs (including runs
started from workspaces and dbt docs jobs), deployed apps, every Airflow component (web,
scheduler, and workers on their worker warehouse) and each layer's running Trino workers (their
minimum is always granted; queries themselves do not count). The Airflow broker and metadata database are
small fixed-size support containers and do not count. Work beyond the limit queues; the dispatcher
starts it in strict first-in, first-out order per warehouse when slots free up, so nothing starves.

Workspaces have their own limits (see workspace_settings) and never count here. Dedicated warehouses
(a team's own, see ComputePool.dedicated) have no limit: their work never queues and counts toward
nothing else; their use is still reported.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select

from .models import ComputePool, Run, Workload, Workspace, PlatformSetting, utcnow

ACTIVE_RUN = ("pending", "running")
LIVE_WORKLOAD = ("live", "deploying")
SUPPORT_SUFFIXES = ("-broker", "-metadata")


def _worker_warehouse(pub: Workload) -> str:
    settings = (pub.program_def or {}).get("airflow_settings") or {}
    return settings.get("worker_warehouse") or pub.warehouse


def workload_held(pub: Workload) -> Dict[str, int]:
    """Containers a deployed workload holds now, per warehouse name."""
    if pub.status not in LIVE_WORKLOAD or pub.kind not in ("app", "airflow"):
        return {}
    refs = [r for r in dict.fromkeys([*(pub.execution_refs or []), pub.execution_ref]) if r]
    held: Dict[str, int] = defaultdict(int)
    for ref in refs:
        if ref.endswith(SUPPORT_SUFFIXES):
            continue
        held[_worker_warehouse(pub) if "-worker-" in ref else pub.warehouse] += 1
    return dict(held)


def demand(pub: Workload, program) -> Dict[str, int]:
    """Containers a version of the workload needs while it runs, per warehouse name."""
    if pub.kind == "dbt":
        return {pub.warehouse: 1}  # the docs job that takes the version live
    if pub.kind not in ("app", "airflow"):
        return {}
    settings = getattr(program, "airflow_settings", None)
    if pub.kind == "airflow" and settings and settings.mode == "distributed":
        need: Dict[str, int] = defaultdict(int)
        need[pub.warehouse] += 2  # web and scheduler
        need[settings.worker_warehouse or pub.warehouse] += settings.workers
        return dict(need)
    return {pub.warehouse: 1}


def extra(needs: Dict[str, int], pub: Optional[Workload]) -> Dict[str, int]:
    """Containers needed beyond what the workload already holds (a replacement or restart needs none)."""
    have = workload_held(pub) if pub is not None else {}
    return {name: n - have.get(name, 0) for name, n in needs.items() if n - have.get(name, 0) > 0}


def _aware(moment):
    from datetime import timezone
    if moment is None:
        return utcnow()
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def held(db) -> Dict[str, int]:
    """Containers running per warehouse name, from the platform's own records."""
    names = {p.id: p.name for p in db.scalars(select(ComputePool))}
    counts: Dict[str, int] = defaultdict(int)
    for pool_id, n in db.execute(select(Run.pool_id, func.count()).where(Run.status.in_(ACTIVE_RUN)).group_by(Run.pool_id)):
        if pool_id in names:
            counts[names[pool_id]] += n
    for pub in db.scalars(select(Workload).where(Workload.status.in_(LIVE_WORKLOAD), Workload.kind.in_(("app", "airflow")))):
        for name, n in workload_held(pub).items():
            counts[name] += n
    from .layer_infrastructure import trino_workers
    for _, name, n in trino_workers(db):
        counts[name] += n
    return dict(counts)


def shortfall(db, needs: Dict[str, int], replacing: Optional[Workload] = None) -> Optional[str]:
    """The first warehouse without room for `needs` (a replaced workload's own containers are free), or None."""
    pools = {p.name: p for p in db.scalars(select(ComputePool).where(ComputePool.name.in_(list(needs))))}
    in_use = held(db)
    released = workload_held(replacing) if replacing is not None else {}
    for name, n in needs.items():
        pool = pools.get(name)
        if pool is None or pool.dedicated:
            continue  # unknown warehouses are refused elsewhere; dedicated ones have no slot limit
        if in_use.get(name, 0) - released.get(name, 0) + n > pool.max_concurrent:
            return name
    return None


def queue(db, pool_name: str) -> List[Tuple[str, str]]:
    """("run"|"workload", id) waiting for this warehouse, oldest first."""
    pool = db.scalar(select(ComputePool).where(ComputePool.name == pool_name))
    items = []
    if pool is not None:
        items += [(r.queued_at, "run", r.id) for r in db.scalars(select(Run).where(Run.status == "queued", Run.pool_id == pool.id))]
    items += [(w.queued_at, "workload", w.id) for w in db.scalars(select(Workload).where(Workload.queued_sha.is_not(None)))
              if pool_name in (w.queued_demand or {})]
    return [(kind, ident) for _, kind, ident in sorted(items, key=lambda i: (_aware(i[0]), i[2]))]


def position(db, kind: str, ident: str, pool_name: Optional[str]) -> Optional[int]:
    if not pool_name:
        return None
    for index, item in enumerate(queue(db, pool_name), start=1):
        if item == (kind, ident):
            return index
    return None


# --- which warehouse work runs on -----------------------------------------------------------------

WAREHOUSES = "warehouses"  # platform setting: {"default": warehouse name}


def trino_warehouses(db) -> Dict[str, List[str]]:
    """Warehouses that run a layer's Trino workers, with those layers' names. Only Trino uses them."""
    from .layer_infrastructure import trino_workers
    out: Dict[str, List[str]] = defaultdict(list)
    for layer, name, _ in trino_workers(db):
        out[name].append(layer.name)
    return dict(out)


def refuse_trino_warehouse(db, name: Optional[str]) -> None:
    from fastapi import HTTPException
    layers = trino_warehouses(db).get(name or "")
    if layers:
        raise HTTPException(422, f"warehouse {name!r} runs the Trino workers of {', '.join(layers)}; only Trino uses it. "
                                 "Choose another warehouse")


def default_warehouse(db) -> Optional[str]:
    stored = db.get(PlatformSetting, WAREHOUSES)
    name = (stored.value or {}).get("default") if stored else None
    return name if name and db.scalar(select(ComputePool.id).where(ComputePool.name == name)) else None


def choose_warehouse(db, explicit: Optional[str], declared: Optional[str] = None) -> Optional[str]:
    """The warehouse for new work: an explicit choice; else the one fdp.json or the workload declares, if it
    exists; else the default warehouse. None when there is nothing to fall back to."""
    if explicit:
        return explicit
    if declared and db.scalar(select(ComputePool.id).where(ComputePool.name == declared)):
        return declared
    return default_warehouse(db)


# --- workspaces: their own limits ---------------------------------------------------------------

WORKSPACES = "workspaces"


def workspace_settings(db) -> dict:
    """{"warehouse": name|None, "max_running": int|None, "max_running_per_user": int}; Helm values are the fallback."""
    from .config import get_settings
    stored = db.get(PlatformSetting, WORKSPACES)
    value = dict(stored.value) if stored else {}
    value.setdefault("warehouse", None)
    value.setdefault("max_running", None)
    value.setdefault("max_running_per_user", get_settings().max_running_workspaces_per_user)
    return value


def workspace_queue(db) -> List[Workspace]:
    return list(db.scalars(select(Workspace).where(Workspace.status == "queued").order_by(Workspace.queued_at, Workspace.id)))


def workspaces_running(db) -> int:
    return db.scalar(select(func.count()).select_from(Workspace).where(Workspace.status.in_(("starting", "running")))) or 0


# --- dispatcher -------------------------------------------------------------------------------

def dispatch(db, orch) -> List[str]:
    """Start queued work that fits now. Returns what was started (for logs and tests)."""
    from .routes.runs import launch_queued_run
    started = []
    in_use = held(db)
    for pool in db.scalars(select(ComputePool).order_by(ComputePool.name)):
        free = pool.max_concurrent - in_use.get(pool.name, 0)
        for kind, ident in queue(db, pool.name):
            if kind == "run":
                if free < 1:
                    break
                run = db.get(Run, ident)
                if launch_queued_run(db, orch, run):
                    started.append("run:" + ident)
                free -= 1
                in_use[pool.name] = in_use.get(pool.name, 0) + 1
            else:
                pub = db.get(Workload, ident)
                from .deploying import _deploying
                if pub.id in _deploying:
                    break  # its deploy is already running; keep the order
                if shortfall(db, pub.queued_demand or {}, replacing=pub):
                    break
                from .deploying import schedule_deploy
                from .db import SessionLocal
                schedule_deploy(SessionLocal, orch, pub.id, pub.queued_sha, "capacity available")
                started.append("workload:" + ident)
                break  # re-evaluate counts on the next tick once the deploy has taken its slots
    settings = workspace_settings(db)
    if settings["max_running"] is not None:
        from .routes.workspaces import launch_workspace
        free = settings["max_running"] - workspaces_running(db)
        for ws in workspace_queue(db):
            if free < 1:
                break
            launch_workspace(db, orch, ws)
            started.append("workspace:" + ws.id)
            free -= 1
    db.commit()
    return started
