from __future__ import annotations

from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditEvent, ComputePool, DataSource, Run
from ..schemas import DataSourceOut, PoolOut, RunOut


def audit(db: Session, actor: str, action: str, target: str, **detail) -> None:
    db.add(AuditEvent(actor=actor, action=action, target=target, detail=detail))


def ds_out(ds: DataSource, db: Optional[Session] = None) -> DataSourceOut:
    out = DataSourceOut.model_validate(ds)
    out.groups = sorted(g.group for g in ds.grants)
    if db is not None:
        from ..rbac import source_resource
        out.resource_id = source_resource(db, ds).id
    return out


def run_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id, owner=run.owner, initiated_by=run.initiated_by, workspace_id=run.workspace_id, kind=run.kind,
        workload_id=run.workload_id, commit_sha=run.commit_sha,
        entrypoint=run.entrypoint, program=run.program, args=run.args or {},
        runtime=run.runtime, runtime_version=run.runtime_version,
        status=run.status, pool_name=run.pool.name,
        created_at=run.created_at, finished_at=run.finished_at, queue_position=run.queue_position,
        waiting_reason=run.waiting_reason if run.status in ("pending", "failed") else None,
    )


def require_warehouse(db: Session, name: str, user) -> ComputePool:
    """A warehouse this identity may put work on: unrestricted, or one of its groups is allowed."""
    pool = db.scalar(select(ComputePool).where(ComputePool.name == name))
    if pool is None or (pool.allowed_groups and not user.is_admin and not set(pool.allowed_groups) & set(user.groups)):
        raise HTTPException(404, f"warehouse {name!r} not available")
    from ..capacity import refuse_trino_warehouse
    refuse_trino_warehouse(db, pool.name)
    return pool


def pools_for(db: Session, groups: List[str]) -> List[PoolOut]:
    pools = db.scalars(select(ComputePool).order_by(ComputePool.cpu)).all()
    return [p for p in describe_pools(db, pools) if not p.allowed_groups or set(p.allowed_groups) & set(groups)]


def describe_pools(db: Session, pools) -> List[PoolOut]:
    """Warehouses with what uses them now, which one is the default and which run Trino workers."""
    from ..capacity import default_warehouse, held, queue, trino_warehouses
    in_use, default, trino = held(db), default_warehouse(db), trino_warehouses(db)
    return [PoolOut.model_validate(p).model_copy(update={"in_use": in_use.get(p.name, 0), "queued": len(queue(db, p.name)),
                                                         "default": p.name == default, "trino_layers": trino.get(p.name, [])})
            for p in pools]
