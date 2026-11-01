"""Admin-only: the data catalog, group grants, compute pools and the audit log."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import User, require_admin
from .. import settings_access
from ..db import get_db
from ..deps import get_orchestrator, get_secret_store
from ..models import AuditEvent, ComputePool, DataSource, Resource, CredentialLease, SqlQuery, Run, utcnow
from ..orchestrator import Orchestrator
from ..schemas import DataSourceIn, DataSourceOut, GrantIn, PoolIn, PoolOut
from ..secrets import SecretStore, datasource_path
from .common import audit, ds_out
from .. import storage_types

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _source(db: Session, name: str) -> DataSource:
    ds = db.scalar(select(DataSource).where(DataSource.name == name))
    if not ds:
        raise HTTPException(404, "data source not found")
    return ds


@router.get("/storage-types")
def storage_type_list(_: User = Depends(require_admin)):
    """Integration types and the settings each requires; the desktop builds its forms from this."""
    return storage_types.public_types()


@router.get("/datasources", response_model=List[DataSourceOut])
def list_sources(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    from ..platform_metadata import SOURCE_ID, authorized
    from ..rbac import resolve_user
    user = resolve_user(db, _)
    result = [ds_out(ds, db) for ds in db.scalars(select(DataSource).order_by(DataSource.name))
              if ds.id != SOURCE_ID or authorized(user)]
    db.commit()   # a source's RBAC object is created on first use
    return result


@router.put("/datasources/{name}", response_model=DataSourceOut)
def upsert_source(
    name: str,
    body: DataSourceIn,
    db: Session = Depends(get_db),
    secrets: SecretStore = Depends(get_secret_store),
    orch: Orchestrator = Depends(get_orchestrator),
    admin: User = Depends(require_admin),
):
    if body.name != name:
        raise HTTPException(422, "name in path and body differ")
    ds = db.scalar(select(DataSource).where(DataSource.name == name)) or DataSource(name=name)
    from ..platform_metadata import SOURCE_ID, SOURCE_NAME
    if ds.id == SOURCE_ID or name == SOURCE_NAME:
        raise HTTPException(409, "Platform metadata is configured through FDP_DATABASE_URL and FDP_METADATA_* deployment settings")
    from ..ownership import require_create
    if not ds.id:
        require_create(db,admin)
    if ds.id and ds.kind != body.kind:
        raise HTTPException(409, "an integration's type cannot change; create a new integration instead")
    stored = {"secret": bool(ds.id and secrets.read(datasource_path(name))),
              "provisioner_secret": bool(ds.id and secrets.read(f"datasources/{name}/provisioner"))}
    from ..layers import canonical, require_layer
    layer = require_layer(db, body.config.get("layer_id") or body.config.get("environment_id") or (ds.config or {}).get("layer_id"))
    environment = layer.id
    requested = body.config
    managed = requested.get('managed_rustfs') is True
    if managed:
        if body.kind != 's3' or body.secret is not None or body.provisioner_secret is not None or body.egress:
            raise HTTPException(422,'managed RustFS file sources use layer credentials and namespace network rules; omit credentials and egress CIDRs')
        from ..managed_files import configuration
        requested = configuration(layer,requested)
        stored['provisioner_secret'] = True  # obtained server-side from the layer, never supplied by callers
    config = storage_types.validate(body.kind, requested, {"secret":body.secret,"provisioner_secret":body.provisioner_secret},stored)
    if managed:
        config['managed_rustfs'] = True
    previous = (ds.config or {}).get("layer_id") or (ds.config or {}).get("environment_id")
    if ds.id and previous and environment != canonical(db, previous):
        raise HTTPException(409, "a data source cannot change layers")
    config["layer_id"] = environment
    if body.config.get("environment_id"):
        config["environment_id"] = body.config["environment_id"]
    from ..config import get_settings
    from ..sql_access import SQL_KINDS, catalog_name
    if body.kind == "s3" and config["bucket"] in storage_types.lake_buckets(db) - {storage_types.bucket_of((ds.config or {}).get("warehouse", ""))}:
        raise HTTPException(422, {"message": "Iceberg table buckets cannot be registered as plain-file sources",
                                  "errors": {"bucket": "this bucket holds Iceberg tables"}})
    if body.kind == "iceberg":
        warehouse = storage_types.bucket_of(config["warehouse"])
        if any(other.config.get("bucket") == warehouse for other in db.scalars(select(DataSource).where(DataSource.kind == "s3"))):
            raise HTTPException(409, {"message": "this bucket is already registered for plain files",
                                      "errors": {"warehouse": "bucket is used by an S3 files integration"}})
    if get_settings().query_executor == "trino":
        if body.kind in SQL_KINDS:
            candidate = DataSource(name=name, config=config)
            catalog = catalog_name(candidate)
            for existing in db.scalars(select(DataSource).where(DataSource.kind.in_(SQL_KINDS))):
                if existing.name != name and catalog_name(existing) == catalog:
                    raise HTTPException(409, "Trino catalog is already mapped to another source")
    changed_connection = ds.id and (ds.kind != body.kind or ds.config != config)
    if changed_connection:
        for query in db.scalars(select(SqlQuery).where(SqlQuery.state.not_in(["FINISHED", "FAILED", "CANCELED"]),
                                                       SqlQuery.expires_at > utcnow())):
            if any(scope.get("source_id") == ds.id for scope in query.scope.values()):
                raise HTTPException(409, "wait for active SQL requests before changing the source connection")
        for run in db.scalars(select(Run).where(Run.status.in_(["pending", "running"]))):
            if (run.access_snapshot or {}).get(ds.id):
                raise HTTPException(409, "wait for admitted data jobs before changing the source connection")
        if db.scalar(select(CredentialLease).where(CredentialLease.source_id == ds.id, CredentialLease.revoked.is_(False))):
            raise HTTPException(409, "wait for native credential leases to end before changing the source connection")
        for schema in db.scalars(select(Resource).where(Resource.kind == "schema", Resource.object_id == ds.id)):
            schema.config = {**schema.config,"state": "pending"}
    ds.kind, ds.description, ds.config, ds.egress = body.kind, body.description, config, body.egress
    db.add(ds)
    if body.secret is not None:
        secrets.put(datasource_path(name), body.secret)
    if body.provisioner_secret is not None:
        secrets.put(f"datasources/{name}/provisioner", body.provisioner_secret)
    from ..layer_infrastructure import _managed
    if not _managed(db, environment):
        orch.sync_data_source_egress(name, [] if get_settings().query_executor == "trino" and body.kind in SQL_KINDS else body.egress)
    audit(db, admin.username, "datasource.upsert", name, secret_changed=body.secret is not None)
    db.flush()
    _sync_catalogs(db, orch, secrets, environment)
    from ..rbac import source_resource, own, has_resource_assignments
    resource = source_resource(db, ds)
    if not has_resource_assignments(db, resource.id):
        own(db, admin, resource)
    db.commit()
    return ds_out(ds, db)


def _sync_catalogs(db, orch, secrets, layer_id):
    """Write the layer's generated source catalogs now; the background pass retries any failure."""
    from ..layers import canonical
    from .. import source_catalogs
    layer = db.get(Resource, canonical(db, layer_id)) if layer_id else None
    if layer is None or not hasattr(orch, 'write_layer_secret'):
        return
    try:
        with db.begin_nested():
            source_catalogs.reconcile_layer(db, orch, secrets, layer)
    except Exception:
        import logging
        logging.getLogger(__name__).warning('source catalogs for layer %s will be written by the next pass', layer.id)


@router.delete("/datasources/{name}", status_code=204)
def delete_source(
    name: str,
    db: Session = Depends(get_db),
    secrets: SecretStore = Depends(get_secret_store),
    orch: Orchestrator = Depends(get_orchestrator),
    admin: User = Depends(require_admin),
):
    ds = _source(db, name)
    from ..platform_metadata import SOURCE_ID
    if ds.id == SOURCE_ID:
        raise HTTPException(409, "The platform metadata integration is deployment managed")
    if db.scalar(select(Resource).where(Resource.kind == "schema", Resource.object_id == ds.id)):
        raise HTTPException(409, "source has governed schemas; retire native access before deleting it")
    if db.scalar(select(CredentialLease).where(CredentialLease.source_id == ds.id, CredentialLease.revoked.is_(False))):
        raise HTTPException(409, "source still has native credential leases")
    from sqlalchemy import delete
    resource = db.scalar(select(Resource).where(Resource.kind == "source", Resource.object_id == ds.id))
    if resource:
        from ..rbac import delete_resource_assignments
        delete_resource_assignments(db, resource.id)
        db.delete(resource)
    db.execute(delete(CredentialLease).where(CredentialLease.source_id == ds.id))
    db.delete(ds)
    secrets.delete(datasource_path(name))
    secrets.delete(f"datasources/{name}/provisioner")
    from ..layer_infrastructure import _managed
    if not _managed(db, (ds.config or {}).get('layer_id')):
        orch.sync_data_source_egress(name, [])
    audit(db, admin.username, "datasource.delete", name)
    db.flush()
    _sync_catalogs(db, orch, secrets, (ds.config or {}).get('layer_id'))
    db.commit()


@router.post("/datasources/{name}/grants", response_model=DataSourceOut)
def grant(name: str, body: GrantIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    raise HTTPException(410, "use RBAC app groups and schema role assignments")


@router.delete("/datasources/{name}/grants/{group}", response_model=DataSourceOut)
def revoke(name: str, group: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    raise HTTPException(410, "use RBAC schema role assignments")


class WorkspaceSettingsIn(BaseModel):
    """Workspaces have their own limits; they never count toward a warehouse's maximum."""
    warehouse: Optional[str] = Field(default=None, description="Warehouse whose size every editor uses; empty keeps the installation default")
    max_running: Optional[int] = Field(default=None, ge=1, description="Workspaces running at once across everyone; more wait in line. Empty: no total limit")
    max_running_per_user: int = Field(ge=1, description="Workspaces one person may have running or waiting")


@router.get("/settings/workspaces")
def workspace_settings(db: Session = Depends(get_db), _: User = Depends(settings_access.needs("set-pools", "view"))):
    from .. import capacity
    return {**capacity.workspace_settings(db), "running": capacity.workspaces_running(db),
            "queued": len(capacity.workspace_queue(db))}


@router.put("/settings/workspaces")
def set_workspace_settings(body: WorkspaceSettingsIn, db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-pools", "manage"))):
    from ..models import PlatformSetting
    from .. import capacity
    chosen = db.scalar(select(ComputePool).where(ComputePool.name == body.warehouse)) if body.warehouse else None
    if body.warehouse and not chosen:
        raise HTTPException(422, f"warehouse {body.warehouse!r} does not exist")
    if chosen is not None and chosen.dedicated:
        raise HTTPException(422, f"{body.warehouse} is dedicated to its groups; choose a shared warehouse for workspaces")
    if chosen is not None:
        capacity.refuse_trino_warehouse(db, chosen.name)
    row = db.get(PlatformSetting, capacity.WORKSPACES) or PlatformSetting(key=capacity.WORKSPACES)
    row.value = body.model_dump()
    db.add(row)
    audit(db, admin.username, "settings.workspaces", capacity.WORKSPACES, **body.model_dump())
    db.commit()
    return workspace_settings(db, admin)


@router.delete("/settings/workspaces", status_code=204)
def reset_workspace_settings(db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-pools", "manage"))):
    """Back to the installation defaults: no workspace warehouse, no total limit, the Helm per-person limit."""
    from ..models import PlatformSetting
    from .. import capacity
    row = db.get(PlatformSetting, capacity.WORKSPACES)
    if row:
        db.delete(row)
    audit(db, admin.username, "settings.workspaces.reset", capacity.WORKSPACES)
    db.commit()


class DefaultWarehouseIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    layer_id: Optional[str] = Field(default=None, description="Only for work in this layer")
    workload_kind: Optional[Literal["script", "notebook", "app", "dbt", "airflow"]] = Field(
        default=None, description="Only for this kind of work")


def _scoped_out(db: Session, entry: dict) -> dict:
    from .. import capacity
    return {"layer_id": entry.get("layer_id"), "layer_name": capacity.layer_name(db, entry["layer_id"]) if entry.get("layer_id") else None,
            "workload_kind": entry.get("workload_kind"), "name": entry["warehouse"]}


@router.get("/settings/default-warehouse")
def default_warehouse_setting(layer_id: Optional[str] = None, workload_kind: Optional[str] = None,
                              db: Session = Depends(get_db), _: User = Depends(settings_access.needs("set-pools", "view"))):
    """Where work without a warehouse runs, and what warehouse choices start with. With layer_id and/or
    workload_kind: that scoped default only (null when none is set). Without: the installation default and
    every scoped default."""
    from .. import capacity
    if layer_id or workload_kind:
        from ..layers import canonical
        layer_id = canonical(db, layer_id) if layer_id else None
        match = next((e for e in capacity.scoped_defaults(db)
                      if e.get("layer_id") == layer_id and e.get("workload_kind") == workload_kind), None)
        return {"name": match["warehouse"] if match else None, "layer_id": layer_id, "workload_kind": workload_kind}
    stored = capacity._setting(db).get("default")
    return {"name": capacity.default_warehouse(db) if stored else None,
            "scoped": [_scoped_out(db, e) for e in capacity.scoped_defaults(db)]}


@router.put("/settings/default-warehouse")
def set_default_warehouse(body: DefaultWarehouseIn, db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-pools", "manage"))):
    from ..models import PlatformSetting
    from .. import capacity
    pool = db.scalar(select(ComputePool).where(ComputePool.name == body.name))
    if pool is None:
        raise HTTPException(422, f"warehouse {body.name!r} does not exist")
    if pool.dedicated:
        raise HTTPException(422, f"{body.name} is dedicated to its groups; choose a shared warehouse as the default")
    capacity.refuse_trino_warehouse(db, pool.name)
    row = db.get(PlatformSetting, capacity.WAREHOUSES) or PlatformSetting(key=capacity.WAREHOUSES)
    value = dict(row.value or {})
    if body.layer_id or body.workload_kind:
        from ..layers import canonical
        layer_id = canonical(db, body.layer_id) if body.layer_id else None
        if not capacity.fits(pool, layer_id, body.workload_kind):
            raise HTTPException(422, f"{body.name} is not meant for that layer or kind of work")
        scoped = [e for e in value.get("scoped") or []
                  if not (e.get("layer_id") == layer_id and e.get("workload_kind") == body.workload_kind)]
        scoped.append({"layer_id": layer_id, "workload_kind": body.workload_kind, "warehouse": pool.name})
        value["scoped"] = scoped
        audit(db, admin.username, "pool.default", pool.name, layer_id=layer_id, workload_kind=body.workload_kind)
        result = {"name": pool.name, "layer_id": layer_id, "workload_kind": body.workload_kind}
    else:
        if pool.layer_ids or pool.workload_kinds:
            raise HTTPException(422, f"{body.name} is limited to layers or kinds of work; the default for all work cannot be")
        value["default"] = pool.name
        audit(db, admin.username, "pool.default", pool.name)
        result = {"name": pool.name}
    row.value = value
    db.add(row)
    db.commit()
    return result


@router.delete("/settings/default-warehouse", status_code=204)
def clear_default_warehouse(layer_id: Optional[str] = None, workload_kind: Optional[str] = None,
                            db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-pools", "manage"))):
    """Clear the installation default, or with layer_id and/or workload_kind only that scoped default."""
    from ..models import PlatformSetting
    from .. import capacity
    row = db.get(PlatformSetting, capacity.WAREHOUSES)
    if row:
        value = dict(row.value or {})
        if layer_id or workload_kind:
            from ..layers import canonical
            layer_id = canonical(db, layer_id) if layer_id else None
            value["scoped"] = [e for e in value.get("scoped") or []
                               if not (e.get("layer_id") == layer_id and e.get("workload_kind") == workload_kind)]
        else:
            value.pop("default", None)
        row.value = value
        if not value.get("default") and not value.get("scoped"):
            db.delete(row)
    audit(db, admin.username, "pool.default.clear", capacity.WAREHOUSES, layer_id=layer_id, workload_kind=workload_kind)
    db.commit()


class EgressHostIn(BaseModel):
    host: str = Field(min_length=1, max_length=253, description='"github.com", or "*.githubusercontent.com" for its subdomains')
    description: str = Field(default="", max_length=200)


class EgressAllowlistIn(BaseModel):
    hosts: List[EgressHostIn] = Field(default_factory=list, max_length=500)


def _egress_out(db: Session) -> dict:
    from .. import egress
    from ..config import get_settings
    return {"hosts": egress.hosts(db), "proxy": bool(get_settings().egress_proxy_url)}


@router.get("/settings/egress")
def egress_allowlist(db: Session = Depends(get_db), _: User = Depends(settings_access.needs("set-network", "view"))):
    """Host names VS Code and RStudio may reach over HTTPS; proxy says whether the installation runs the proxy."""
    return _egress_out(db)


@router.put("/settings/egress")
def set_egress_allowlist(body: EgressAllowlistIn, db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-network", "manage"))):
    from .. import egress
    entries, seen = [], set()
    for item in body.hosts:
        host = item.host.strip().lower().rstrip(".")
        if not egress.valid_host(host):
            raise HTTPException(422, f"{item.host!r} is not a host name; use github.com or *.githubusercontent.com")
        from ..egress_proxy import package_index
        if package_index(host):
            raise HTTPException(422, f"{host} is a public package index; workspaces install packages from the platform's "
                                     "package repository (Packages), where Publishers import what is needed")
        if host in seen:
            raise HTTPException(422, f"{host} is listed twice")
        seen.add(host)
        entries.append({"host": host, "description": item.description.strip()})
    egress.set_hosts(db, entries)
    audit(db, admin.username, "settings.egress", egress.KEY, hosts=[e["host"] for e in entries])
    db.commit()
    return _egress_out(db)


def _scope(db: Session, name: str, body: PoolIn, engines: List[str]):
    """Validated (layer ids, kinds) for a warehouse. Scoping must not strand what already relies on it."""
    from .. import capacity
    from ..layers import canonical
    from ..models import Workload
    layer_ids = list(dict.fromkeys(canonical(db, ident) for ident in body.layer_ids))
    kinds = list(dict.fromkeys(body.workload_kinds))
    if not layer_ids and not kinds:
        return layer_ids, kinds
    if capacity.default_warehouse(db) == name:
        raise HTTPException(422, "this is the default warehouse for all work, so it cannot be limited to layers or kinds; "
                                 "set a default per layer or kind instead")
    if capacity.workspace_settings(db)["warehouse"] == name:
        raise HTTPException(422, "workspaces of every layer use this warehouse; it cannot be limited to layers or kinds")
    if engines:
        from ..layer_infrastructure import trino_workers
        trino_layers = {layer.id for layer, warehouse, _ in trino_workers(db) if warehouse == name}
        if kinds or (layer_ids and not trino_layers <= set(layer_ids)):
            raise HTTPException(422, f"Trino workers of {', '.join(engines)} use this warehouse; it may only be limited "
                                     "to their layers, not to kinds of work")
    candidate = ComputePool(name=name, layer_ids=layer_ids, workload_kinds=kinds)
    from .. import rbac
    from ..layers import info
    stranded = []
    for w in db.scalars(select(Workload).where(Workload.status != "stopped")):
        worker = ((w.program_def or {}).get("airflow_settings") or {}).get("worker_warehouse")
        if name in (w.warehouse, worker) and not capacity.fits(candidate, info(db, rbac.workload_resource(db, w))["layer_id"], w.kind):
            stranded.append(w.slug)
    if stranded:
        raise HTTPException(409, f"deployed workloads use this warehouse outside that scope: {', '.join(stranded[:5])}"
                                 + (" and more" if len(stranded) > 5 else "") + "; move them first")
    for entry in capacity.scoped_defaults(db):
        if entry["warehouse"] == name and not capacity.fits(candidate, entry.get("layer_id"), entry.get("workload_kind")):
            raise HTTPException(422, "this warehouse is a default outside that scope; change that default first")
    return layer_ids, kinds


@router.get("/pools", response_model=List[PoolOut])
def list_pools(db: Session = Depends(get_db), _: User = Depends(settings_access.needs("set-pools", "view"))):
    from .common import describe_pools
    return describe_pools(db, db.scalars(select(ComputePool).order_by(ComputePool.cpu)))


@router.put("/pools/{name}", response_model=PoolOut)
def upsert_pool(name: str, body: PoolIn, db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-pools", "manage"))):
    if body.name != name:
        raise HTTPException(422, "name in path and body differ")
    pool = db.scalar(select(ComputePool).where(ComputePool.name == name)) or ComputePool()
    from ..layer_infrastructure import MIN_WORKER_MEMORY_GB, trino_workers
    engines = [layer.name for layer, warehouse, _ in trino_workers(db) if warehouse == name]
    if engines and body.memory_gb < MIN_WORKER_MEMORY_GB:
        raise HTTPException(422, f"Trino workers of {', '.join(engines)} use this warehouse; it needs at least {MIN_WORKER_MEMORY_GB} GB memory")
    if body.dedicated:
        from ..capacity import default_warehouse, workspace_settings
        if default_warehouse(db) == name:
            raise HTTPException(422, "this is the default warehouse; a dedicated warehouse is for its groups' work")
        if not body.allowed_groups:
            raise HTTPException(422, "a dedicated warehouse belongs to groups: name at least one in allowed_groups")
        if engines:
            raise HTTPException(422, f"Trino workers of {', '.join(engines)} use this warehouse; a dedicated warehouse is for its groups' work")
        if workspace_settings(db)["warehouse"] == name:
            raise HTTPException(422, "workspaces use this warehouse; a dedicated warehouse is for its groups' work")
    values = body.model_dump()
    values["layer_ids"], values["workload_kinds"] = _scope(db, name, body, engines)
    if not pool.id:
        from ..ownership import require_create
        pool.owner_group_id = require_create(db,admin).id
    for field, value in values.items():
        setattr(pool, field, value)
    db.add(pool)
    audit(db, admin.username, "pool.upsert", name)
    db.commit()
    return pool


@router.delete("/pools/{name}", status_code=204)
def delete_pool(name: str, db: Session = Depends(get_db), admin: User = Depends(settings_access.needs("set-pools", "manage"))):
    pool = db.scalar(select(ComputePool).where(ComputePool.name == name))
    if not pool:
        raise HTTPException(404, "pool not found")
    if db.scalar(select(Run.id).where(Run.pool_id == pool.id).limit(1)):
        raise HTTPException(409, "this warehouse has run history; keep it, or stop using it in configuration")
    from ..layer_infrastructure import trino_workers
    engines = [layer.name for layer, warehouse, _ in trino_workers(db) if warehouse == name]
    if engines:
        raise HTTPException(409, f"Trino workers of {', '.join(engines)} use this warehouse; choose another in their layer first")
    from ..capacity import default_warehouse, workspace_settings
    if workspace_settings(db)["warehouse"] == name:
        raise HTTPException(409, "workspaces use this warehouse; choose another in the workspace settings first")
    if default_warehouse(db) == name:
        raise HTTPException(409, "this is the default warehouse; choose another default first")
    from ..capacity import scoped_defaults
    if any(e["warehouse"] == name for e in scoped_defaults(db)):
        raise HTTPException(409, "this warehouse is a default for a layer or kind of work; choose another default first")
    db.delete(pool)
    audit(db, admin.username, "pool.delete", name)
    db.commit()


@router.get("/audit")
def audit_log(limit: int = 200, actor: str = "", action: str = "", target: str = "",
              db: Session = Depends(get_db), _: User = Depends(settings_access.needs("set-auditlog", "view"))):
    """The newest events first; filter by who (part of the name), action (prefix) and object (its ID)."""
    query = select(AuditEvent).order_by(AuditEvent.id.desc())
    if actor:
        query = query.where(AuditEvent.actor.ilike(f"%{actor}%"))
    if action:
        query = query.where(AuditEvent.action.like(f"{action}%"))
    if target:
        query = query.where(AuditEvent.target == target)
    events = db.scalars(query.limit(max(1, min(limit, 1000))))
    return [{"at": e.at, "actor": e.actor, "action": e.action, "target": e.target, "detail": e.detail}
            for e in events]
