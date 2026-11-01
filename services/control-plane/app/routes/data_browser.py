"""On-demand, identity-scoped metadata. Never crawl sources or return sample data."""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from .. import rbac, sql_access
from ..auth import User, get_current_user
from ..config import get_settings
from ..db import get_db
from ..deps import get_secret_store
from ..models import DataSource, Resource
from .sql import QueryIn, query_request, transaction_call

router = APIRouter(prefix="/v1/data", tags=["data browser"])


@router.get("/schemas/{resource_id}/preview")
async def preview(resource_id: str, request: Request, response: Response,
                  table: str = Query(min_length=1, max_length=1024), db=Depends(get_db),
                  user: User = Depends(get_current_user), authorization: str = Header(default="")):
    response.headers["Cache-Control"] = "no-store"
    source, schema = await run_in_threadpool(transaction_call, db, scope, resource_id, user)
    if source.kind not in sql_access.SQL_KINDS:
        raise HTTPException(409, "table preview supports PostgreSQL and Iceberg")
    def identifier(value):
        return '"' + value.replace('"', '""') + '"'
    statement = "SELECT * FROM " + ".".join(identifier(v) for v in (sql_access.catalog_name(source), schema.name, table)) + " LIMIT 25"
    result = await query_request(QueryIn(source=source.name, sql=statement, max_rows=25),
        request, db=db, authorization=authorization, x_fdp_viewer="")
    def json_safe(value):
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 9007199254740991:
            return str(value)
        if isinstance(value, list):
            return [json_safe(v) for v in value]
        if isinstance(value, dict):
            return {k: json_safe(v) for k, v in value.items()}
        return value
    return {"columns": result["columns"], "rows": json_safe(result["rows"][:25]), "limit": 25, "query_id": result["query_id"]}


def scope(db, resource_id, user):
    schema = db.get(Resource, resource_id)
    source = db.get(DataSource, schema.object_id) if schema and schema.kind == "schema" else None
    # Management/recovery authority is not a data-role bypass, even for metadata.
    role = rbac.data_scope(db, user, source).get(schema.name) if source else None
    if not role:
        raise HTTPException(404, "schema not available with this data identity")
    if schema.config.get("state") != "ready":
        raise HTTPException(409, "set up schema access before browsing its objects")
    return source, schema


def storage_client(db, source, schema, store, reserved=frozenset()):
    """An S3 client with temporary read-only credentials narrowed to this registered namespace, and the namespace's
    root key. reserved: Iceberg table buckets, which are never read as plain files."""
    import boto3
    from botocore.config import Config
    from .. import storage_access
    cfg = source.config
    if cfg.get("bucket") in reserved:
        raise HTTPException(409, "Iceberg buckets are accessible only through governed SQL")
    root = storage_access.prefix_path(cfg.get("prefix", ""), schema.config.get("prefix", schema.name))
    cred, _ = storage_access.credentials(source, {schema.name: schema}, {schema.name: "Reader"}, store, db=db)
    client = boto3.client("s3", endpoint_url=cfg.get("endpoint_url"), region_name=cfg.get("region", "us-east-1"),
        aws_access_key_id=cred["access_key_id"], aws_secret_access_key=cred["secret_access_key"],
        aws_session_token=cred["session_token"],
        config=Config(connect_timeout=10, read_timeout=15, retries={"max_attempts": 1}, s3={"addressing_style": "path"}))
    return client, cfg["bucket"], root


def storage_children(db, source, schema, path, after, limit, store, reserved=frozenset()):
    """reserved: Iceberg table buckets, which are never listed as plain files."""
    from botocore.exceptions import BotoCoreError, ClientError
    from .. import storage_access

    relative = storage_access.prefix_path("", path)
    try:
        client, bucket, root = storage_client(db, source, schema, store, reserved)
        prefix = storage_access.prefix_path(root, relative)
        prefix = prefix + "/" if prefix else ""
        result = client.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/", MaxKeys=limit,
                                       **({"ContinuationToken": after} if after else {}))
    except (BotoCoreError, ClientError, KeyError):
        raise HTTPException(502, "storage listing failed; check namespace setup and source connectivity") from None
    children = []
    for folder in result.get("CommonPrefixes", []):
        key = folder["Prefix"]
        if not key.startswith(prefix):
            continue
        name = key[len(prefix):].rstrip("/")
        children.append({"name": name, "kind": "folder", "path": storage_access.prefix_path(relative, name)})
    for obj in result.get("Contents", []):
        key = obj["Key"]
        if not key.startswith(prefix) or key == prefix:
            continue
        name = key[len(prefix):]
        children.append({"name": name, "kind": "file", "path": storage_access.prefix_path(relative, name),
                         "size": obj.get("Size"), "modified_at": obj["LastModified"].isoformat() if obj.get("LastModified") else None})
    return {"children": children, "next_cursor": result.get("NextContinuationToken")}


RELATION_KINDS = ("table", "view", "materialized_view")


def indexed_children(db, schema, table=None):
    """The last listing the platform saw (catalog index), returned without asking the engine.
    The app shows it at once and replaces it with the live listing; empty when nothing was seen yet."""
    from sqlalchemy import select
    from ..catalog import asset_id
    from ..models import CatalogAsset
    if table is not None:
        asset = db.get(CatalogAsset, asset_id(schema.id, "object:" + table))
        columns = sorted((asset.attributes or {}).get("columns", []) if asset else [], key=lambda c: c.get("ordinal_position") or 0)
        items = [{"name": c["name"], "kind": "column", "data_type": c.get("data_type"), "nullable": c.get("nullable"),
                  "ordinal_position": c.get("ordinal_position")} for c in columns if c.get("name")]
    else:
        assets = db.scalars(select(CatalogAsset).where(CatalogAsset.resource_id == schema.id, CatalogAsset.kind.in_(RELATION_KINDS)))
        items = sorted(({"name": a.name, "kind": a.kind} for a in assets
                        if (a.attributes or {}).get("object_path") == a.name), key=lambda item: item["name"])
    return {"children": items, "next_cursor": None, "cached": True}


@router.get("/schemas/{resource_id}/children")
async def children(resource_id: str, request: Request, response: Response,
                   table: Optional[str] = Query(default=None, max_length=1024),
                   path: str = Query(default="", max_length=4096), after: str = Query(default="", max_length=4096),
                   limit: int = Query(default=200, ge=1, le=500),
                   cached: bool = Query(default=False, description="Return the last listing the platform saw, without querying the engine"),
                   db=Depends(get_db),
                   user: User = Depends(get_current_user), authorization: str = Header(default=""),
                   store=Depends(get_secret_store)):
    response.headers["Cache-Control"] = "no-store"
    source, schema = await run_in_threadpool(transaction_call, db, scope, resource_id, user)
    if source.kind == "s3":
        if table is not None:
            raise HTTPException(422, "storage namespaces contain folders and files")
        from ..storage_types import lake_buckets
        reserved = await run_in_threadpool(transaction_call, db, lake_buckets)
        result = await run_in_threadpool(transaction_call, db, storage_children, source, schema, path, after, limit, store, reserved)
        from ..catalog import index_children
        await run_in_threadpool(transaction_call, db, index_children, schema, result["children"], None, path)
        return result
    if source.kind not in sql_access.SQL_KINDS:
        raise HTTPException(409, "metadata browsing supports PostgreSQL, Iceberg, and S3 sources")
    if path:
        raise HTTPException(422, "SQL schemas do not have storage paths")
    if cached:
        return await run_in_threadpool(transaction_call, db, indexed_children, schema, table)
    catalog = '"' + sql_access.catalog_name(source).replace('"', '""') + '"'
    if table is None:
        # Names only: asking information_schema.tables for table_type makes Trino load every Iceberg table's
        # metadata (seconds per table). Views come from information_schema.views, which lists only views.
        # Two single-table queries: metadata tables are read on the coordinator, a join would need a worker.
        statement = (f"SELECT table_name FROM {catalog}.information_schema.tables "
                     f"WHERE table_schema = ? AND table_name > ? ORDER BY table_name LIMIT {limit + 1}")
        parameters = [schema.name, after]
        views = await query_request(QueryIn(source=source.name, sql=f"SELECT table_name FROM {catalog}.information_schema.views "
                                            f"WHERE table_schema = ?", parameters=[schema.name], max_rows=10000),
                                    request, db=db, authorization=authorization, x_fdp_viewer="")
        view_names = {row[0] for row in views["rows"]}
    else:
        try:
            position = int(after or "0")
            if position < 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "invalid column cursor") from None
        statement = (f"SELECT column_name, data_type, is_nullable, ordinal_position FROM {catalog}.information_schema.columns "
                     f"WHERE table_schema = ? AND table_name = ? AND ordinal_position > ? ORDER BY ordinal_position LIMIT {limit + 1}")
        parameters = [schema.name, table, position]
    # Reuse admission, Trino policy, auditing, timeouts, and cancellation. No native credential path.
    result = await query_request(QueryIn(source=source.name, sql=statement, parameters=parameters, max_rows=limit + 1),
                                 request, db=db, authorization=authorization, x_fdp_viewer="")
    rows = result["rows"][:limit]
    if table is None:
        items = [{"name": row[0], "kind": "view" if row[0] in view_names else "table"} for row in rows]
        cursor = rows[-1][0] if rows and len(result["rows"]) > limit else None
    else:
        items = [{"name": row[0], "kind": "column", "data_type": row[1], "nullable": row[2] == "YES",
                  "ordinal_position": row[3]} for row in rows]
        cursor = str(rows[-1][3]) if rows and len(result["rows"]) > limit else None
    from ..catalog import index_children
    await run_in_threadpool(transaction_call, db, index_children, schema, items, table, path)
    return {"children": items, "next_cursor": cursor}



@router.get("/schemas/{resource_id}/file")
async def file_preview(resource_id: str, response: Response, path: str = Query(min_length=1, max_length=4096),
                       db=Depends(get_db), user: User = Depends(get_current_user), store=Depends(get_secret_store)):
    """The start of one file, read with your data permissions: the first rows of CSV, JSON lines and Parquet, formatted
    JSON, text or an image. Size-capped and recorded in the audit log; files are never downloaded whole."""
    from ..file_preview import preview
    from ..storage_types import lake_buckets
    from .common import audit
    response.headers["Cache-Control"] = "no-store"
    source, schema = await run_in_threadpool(transaction_call, db, scope, resource_id, user)
    if source.kind != "s3":
        raise HTTPException(409, "file preview is for S3 files namespaces")
    reserved = await run_in_threadpool(transaction_call, db, lake_buckets)
    client, bucket, root = await run_in_threadpool(transaction_call, db, storage_client, source, schema, store, reserved)
    result = await run_in_threadpool(preview, client, bucket, root, path)
    def record(db):
        resolved = rbac.resolve_user(db, user)
        audit(db, resolved.username, "storage.file_preview", schema.id, path=result["path"], bytes=result["read"], kind=result["kind"])
        db.commit()
    await run_in_threadpool(transaction_call, db, record)
    return result
