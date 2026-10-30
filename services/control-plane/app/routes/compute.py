"""Warehouse overview: what uses each warehouse, per layer, against its limit.

Counts include everyone's work; details are shown only for work the viewer may already see
(their own runs and workspaces, workloads they can browse). The rest is summarized as "others".
"""
from collections import defaultdict
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import capacity, rbac
from ..auth import User, get_current_user
from ..db import get_db
from ..models import ComputePool, Resource, Run, Workload, Workspace
from .common import pools_for

router = APIRouter(prefix="/v1/compute", tags=["compute"])


class Occupant(BaseModel):
    category: str                     # job | app | airflow | trino | workspace
    kind: str                         # script, notebook, dbt, app, airflow, or the editor
    state: str                        # running | queued
    slots: int                        # warehouse slots it takes (workspaces take none)
    name: str
    owner: str
    layer_id: Optional[str] = None
    layer_name: Optional[str] = None
    queue_position: Optional[int] = None
    run_id: Optional[str] = None
    workload_id: Optional[str] = None
    workspace_id: Optional[str] = None


class LayerUse(BaseModel):
    layer_id: Optional[str] = None
    layer_name: Optional[str] = None
    in_use: int = 0
    queued: int = 0


class KindUse(BaseModel):
    kind: str                         # script, notebook, app, dbt, airflow, or workers (Trino)
    in_use: int = 0
    queued: int = 0


class WorkspaceUse(BaseModel):
    running: int
    queued: int
    max_running: Optional[int] = None


class WarehouseOverview(BaseModel):
    name: str
    cpu: float
    memory_gb: float
    gpu: int
    max_concurrent: int
    # Dedicated to these groups: no slot limit, never queues (max_concurrent does not apply).
    dedicated: bool = False
    allowed_groups: List[str] = []
    # Scope: empty means every layer / every kind of work.
    layer_ids: List[str] = []
    layer_names: List[str] = []
    workload_kinds: List[str] = []
    in_use: int
    queued: int
    layers: List[LayerUse]
    kinds: List[KindUse] = []
    occupants: List[Occupant]
    others_in_use: int = 0            # slots taken by work the viewer may not see
    others_queued: int = 0
    workspaces: Optional[WorkspaceUse] = None  # set on the warehouse that sizes workspaces


def _layer(db, ident) -> tuple:
    from ..layers import canonical
    try:
        layer_id = canonical(db, ident)
    except HTTPException:
        return None, None
    layer = db.get(Resource, layer_id) if layer_id else None
    return (layer_id or None), (layer.name if layer else None)


def _run_visible(db, user, run, browsable) -> bool:
    if run.workload_id and run.workload_id not in browsable:
        return False
    from .runs import owned_run
    try:
        owned_run(db, run.id, user)
        return True
    except HTTPException:
        return False


@router.get("/warehouses", response_model=List[WarehouseOverview])
def warehouses(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    user = rbac.resolve_user(db, user)
    from ..layers import info
    from ..ownership import browsing_allowed
    pools = list(db.scalars(select(ComputePool).order_by(ComputePool.cpu))) if user.is_admin else pools_for(db, user.groups)
    names = {p.name for p in pools}
    workloads = list(db.scalars(select(Workload)))
    resources = {w.id: rbac.workload_resource(db, w) for w in workloads}
    browsable = {w.id for w in workloads if user.is_admin or browsing_allowed(db, user, resources[w.id])}
    titles = {w.id: w.title for w in workloads}
    seen: Dict[str, List[tuple]] = defaultdict(list)  # pool -> (occupant, visible)

    for run in db.scalars(select(Run).where(Run.status.in_((*capacity.ACTIVE_RUN, "queued")))):
        if run.pool is None or run.pool.name not in names:
            continue
        layer_id, layer_name = _layer(db, run.environment_id)
        queued = run.status == "queued"
        seen[run.pool.name].append((Occupant(
            category="job", kind=run.kind, state="queued" if queued else "running", slots=1,
            name=titles.get(run.workload_id) or run.program or run.entrypoint, owner=run.initiated_by or run.owner,
            layer_id=layer_id, layer_name=layer_name, queue_position=run.queue_position if queued else None,
            run_id=run.id, workload_id=run.workload_id), _run_visible(db, user, run, browsable)))

    for pub in workloads:
        held, waiting = capacity.workload_held(pub), (pub.queued_demand or {}) if pub.queued_sha else {}
        if not held and not waiting:
            continue
        where = info(db, resources[pub.id])
        for state, per_pool in (("running", held), ("queued", waiting)):
            for name, n in per_pool.items():
                if name in names and n:
                    seen[name].append((Occupant(
                        category=pub.kind if pub.kind in ("app", "airflow") else "job", kind=pub.kind, state=state, slots=n,
                        name=pub.title, owner=pub.owner, layer_id=where["layer_id"], layer_name=where["layer_name"],
                        queue_position=pub.queue_position if state == "queued" else None, workload_id=pub.id), pub.id in browsable))

    from ..layer_infrastructure import trino_workers
    for layer, name, count in trino_workers(db):  # layer infrastructure: everyone may see what it takes
        if name in names:
            seen[name].append((Occupant(category="trino", kind="workers", state="running", slots=count,
                                        name="Trino workers", owner="platform", layer_id=layer.id, layer_name=layer.name), True))

    settings = capacity.workspace_settings(db)
    from ..layers import all_layers
    for layer in all_layers(db):
        infrastructure = layer.config.get('infrastructure') or {}
        kafka = infrastructure.get('kafka') or {}
        name = kafka.get('warehouse')
        if name in names and infrastructure.get('enabled') and not infrastructure.get('retired') and kafka.get('enabled'):
            seen[name].append((Occupant(category='kafka', kind='brokers', state='running', slots=kafka.get('replicas',1),
                name='Kafka brokers', owner='platform', layer_id=layer.id, layer_name=layer.name), True))
    if settings["warehouse"] in names:
        for ws in db.scalars(select(Workspace).where(Workspace.status.in_(("queued", "starting", "running")))):
            seen[settings["warehouse"]].append((Occupant(
                category="workspace", kind=ws.editor, state="queued" if ws.status == "queued" else "running", slots=0,
                name=ws.name, owner=ws.owner, layer_id=ws.layer_id, layer_name=ws.layer_name,
                queue_position=ws.queue_position, workspace_id=ws.id), user.is_admin or rbac.same_identity(ws, user)))

    in_use = capacity.held(db)
    out = []
    for pool in pools:
        layers: Dict[Optional[str], LayerUse] = {}
        kinds: Dict[str, KindUse] = {}
        visible, others_in_use, others_queued = [], 0, 0
        for occupant, shown in seen.get(pool.name, []):
            if occupant.category != "workspace":
                use = layers.setdefault(occupant.layer_id, LayerUse(layer_id=occupant.layer_id, layer_name=occupant.layer_name))
                by_kind = kinds.setdefault(occupant.kind, KindUse(kind=occupant.kind))
                if occupant.state == "running":
                    use.in_use += occupant.slots
                    by_kind.in_use += occupant.slots
                else:
                    use.queued += 1
                    by_kind.queued += 1
            if shown:
                visible.append(occupant)
            elif occupant.category != "workspace":
                others_in_use += occupant.slots if occupant.state == "running" else 0
                others_queued += 1 if occupant.state == "queued" else 0
        visible.sort(key=lambda o: (o.state != "running", o.queue_position or 0, o.category, o.name))
        workspaces = None
        if settings["warehouse"] == pool.name:
            workspaces = WorkspaceUse(running=capacity.workspaces_running(db), queued=len(capacity.workspace_queue(db)),
                                      max_running=settings["max_running"])
        out.append(WarehouseOverview(
            name=pool.name, cpu=pool.cpu, memory_gb=pool.memory_gb, gpu=pool.gpu, max_concurrent=pool.max_concurrent,
            dedicated=pool.dedicated, allowed_groups=pool.allowed_groups or [],
            layer_ids=pool.layer_ids or [], layer_names=[capacity.layer_name(db, l) for l in pool.layer_ids or []],
            workload_kinds=pool.workload_kinds or [], kinds=sorted(kinds.values(), key=lambda k: k.kind),
            in_use=in_use.get(pool.name, 0), queued=len(capacity.queue(db, pool.name)),
            layers=sorted(layers.values(), key=lambda l: l.layer_name or ""), occupants=visible,
            others_in_use=others_in_use, others_queued=others_queued, workspaces=workspaces))
    return out
