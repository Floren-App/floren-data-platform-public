"""Called from inside workspaces/compute (via the SDK) to get credentials for a granted source."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Execution, get_execution
from ..db import get_db
from ..deps import get_secret_store, granted_sources
from ..models import DataSource
from ..schemas import ConnectOut, DataSourceOut
from ..secrets import SecretStore
from .common import audit, ds_out

router = APIRouter(prefix="/v1/connect", tags=["connect"])


@router.get("", response_model=List[DataSourceOut])
def available(db: Session = Depends(get_db), wl: Execution = Depends(get_execution)):
    from ..data_access import scope_for_execution
    return [ds_out(ds) for ds in db.scalars(select(DataSource)) if scope_for_execution(db, wl, ds)]


@router.get("/{name}", response_model=ConnectOut)
def connect(
    name: str,
    schema: str = "",
    db: Session = Depends(get_db),
    secrets: SecretStore = Depends(get_secret_store),
    wl: Execution = Depends(get_execution),
    authorization: str = Header(default=""),
    x_fdp_viewer: str = Header(default=""),
):
    ds = db.scalar(select(DataSource).where(DataSource.name == name))
    from ..data_access import scope_for_execution, credentials
    from ..config import get_settings
    from ..sql_access import SQL_KINDS, catalog_name
    scope = scope_for_execution(db, wl, ds) if ds else {}
    if x_fdp_viewer and ds and get_settings().query_executor == "trino" and ds.kind in SQL_KINDS:
        from ..sql_access import actor
        from ..rbac import data_scope
        who = actor(db, authorization, x_fdp_viewer)
        scope = data_scope(db, who.user, ds)
    if schema:
        scope = ({schema: scope[schema], **{k: v for k, v in scope.items() if k != schema and v in ("Reader", "Contributor")}}
                 if schema in scope else {})
    allowed = bool(scope)
    audit(db, wl.username, "datasource.connect" if allowed else "datasource.denied", name,
          workspace=wl.workspace_id, run=wl.run_id, scope=scope)
    db.commit()
    if not allowed:
        raise HTTPException(404, f"data source {name!r} not available")
    from ..storage_types import lake_buckets
    if get_settings().query_executor == "trino" and ds.kind == "s3" and ds.config.get("bucket") in lake_buckets(db):
        raise HTTPException(409, "Iceberg buckets are accessible only through governed SQL")
    if get_settings().query_executor == "trino" and ds.kind in SQL_KINDS:
        # Connection metadata only. No backend password, storage key, or native lease.
        from urllib.parse import urlsplit
        endpoint = urlsplit(get_settings().internal_url)
        return ConnectOut(name=ds.name, kind="trino", config={
            "endpoint": get_settings().internal_url, "host": endpoint.hostname,
            "port": endpoint.port or (443 if endpoint.scheme == "https" else 80),
            "http_scheme": endpoint.scheme, "catalog": catalog_name(ds), "schema": schema or next(iter(scope)),
            "source_kind": ds.kind}, credentials={"username": wl.principal_id or wl.username,
            "token": authorization.split(" ", 1)[1]}, access={"schemas": scope, "executor": "trino"})
    cred, access = credentials(db, ds, scope, secrets, wl.username, wl.run_id)
    return ConnectOut(name=ds.name, kind=ds.kind, config=ds.config, credentials=cred, access=access)
