from __future__ import annotations

from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditEvent, ComputePool, DataSource, Run
from ..schemas import DataSourceOut, PoolOut, RunOut


def audit(db: Session, actor: str, action: str, target: str, **detail) -> None:
    db.add(AuditEvent(actor=actor, action=action, target=target, detail=detail))


def ds_out(ds: DataSource, db: Optional[Session] = None, user=None) -> DataSourceOut:
    out = DataSourceOut.model_validate(ds)
    out.groups = sorted(g.group for g in ds.grants)
    if db is not None:
        from ..rbac import source_resource
        resource=source_resource(db,ds)
        out.resource_id = resource.id
        if user:
            from ..permissions import domain_allowed
            out.capabilities=[a for a in ('storage.connection.read','storage.connection.edit',
                'storage.connection.credentials.change','storage.connection.destination.change','storage.connection.trust.change',
                'storage.sources.deregister') if domain_allowed(db,user,a,resource,'read' if a.endswith('.read') else 'assign')]
    return out


def run_out(run: Run, db=None, user=None) -> RunOut:
    from ..run_access import capabilities
    return RunOut(
        capabilities=capabilities(db,user,run) if db is not None and user is not None else [],
        id=run.id, owner=run.owner, initiated_by=run.initiated_by, workspace_id=run.workspace_id, kind=run.kind,
        workload_id=run.workload_id, commit_sha=run.commit_sha,
        entrypoint=run.entrypoint, program=run.program, args=run.args or {},
        runtime=run.runtime, runtime_version=run.runtime_version,
        status=run.status, pool_name=run.pool.name,
        created_at=run.created_at, finished_at=run.finished_at, queue_position=run.queue_position,
        waiting_reason=run.waiting_reason if run.status in ("pending", "failed") else None,
        layer_id=run.layer_id,
    )


def require_warehouse(db: Session, name: str, user, layer_id=None) -> ComputePool:
    """A warehouse this identity may put work on: unrestricted, or one of its groups is allowed."""
    from .. import compute_access
    pool = db.scalar(select(ComputePool).where(ComputePool.name == name))
    if pool is None or not compute_access.allowed(db,user,"compute.pools.use",pool,layer_id or user.environment_id):
        raise HTTPException(404, f"warehouse {name!r} not available")
    from ..capacity import refuse_trino_warehouse
    refuse_trino_warehouse(db, pool.name)
    return pool


def pools_for(db: Session, groups: List[str], layer_id: Optional[str] = None, kind: Optional[str] = None, user=None) -> List[PoolOut]:
    """Warehouses these groups may use; with a layer and/or kind, only those meant for that work."""
    from ..capacity import fits
    pools = [p for p in db.scalars(select(ComputePool).order_by(ComputePool.cpu)).all() if fits(p, layer_id, kind)]
    from .. import compute_access
    visible={p.id for p in pools if (compute_access.visible(db,user,p,layer_id) if user else not p.allowed_groups or set(p.allowed_groups)&set(groups))}
    return [p for p in describe_pools(db,pools) if p.id in visible]


def describe_pools(db: Session, pools) -> List[PoolOut]:
    """Warehouses with what uses them now, which one is the default and which run Trino workers."""
    from ..capacity import default_warehouse, held, infrastructure_layers, layer_name, queue, scoped_defaults, trino_warehouses
    in_use, default, trino, scoped = held(db), default_warehouse(db), trino_warehouses(db), scoped_defaults(db)
    infrastructure = infrastructure_layers(db)
    def default_for(name):
        return [{"layer_id": e.get("layer_id"), "layer_name": layer_name(db, e["layer_id"]) if e.get("layer_id") else None,
                 "workload_kind": e.get("workload_kind")} for e in scoped if e["warehouse"] == name]
    return [PoolOut.model_validate(p).model_copy(update={
                "in_use": in_use.get(p.name, 0), "queued": len(queue(db, p.name)), "default": p.name == default,
                "trino_layers": trino.get(p.name, []), "infrastructure_layer_ids": infrastructure.get(p.name, []), "layer_names": [layer_name(db, l) for l in p.layer_ids or []],
                "default_for": default_for(p.name)})
            for p in pools]
