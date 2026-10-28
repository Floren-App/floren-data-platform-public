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


def storage_children(db, source, schema, path, after, limit, store, reserved=frozenset()):
    """reserved: Iceberg table buckets, which are never listed as plain files."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
    from .. import storage_access

    cfg = source.config
    if cfg.get("bucket") in reserved:
        raise HTTPException(409, "Iceberg buckets are accessible only through governed SQL")
    relative = storage_access.prefix_path("", path)
    root = storage_access.prefix_path(cfg.get("prefix", ""), schema.config.get("prefix", schema.name))
    prefix = storage_access.prefix_path(root, relative)
    prefix = prefix + "/" if prefix else ""
    # Temporary read-only credentials are narrowed to this registered namespace.
    cred, _ = storage_access.credentials(source, {schema.name: schema}, {schema.name: "Reader"}, store, db=db)
    try:
        client = boto3.client("s3", endpoint_url=cfg.get("endpoint_url"), region_name=cfg.get("region", "us-east-1"),
            aws_access_key_id=cred["access_key_id"], aws_secret_access_key=cred["secret_access_key"],
            aws_session_token=cred["session_token"],
            config=Config(connect_timeout=10, read_timeout=15, retries={"max_attempts": 1}, s3={"addressing_style": "path"}))
        result = client.list_objects_v2(Bucket=cfg["bucket"], Prefix=prefix, Delimiter="/", MaxKeys=limit,
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


@router.get("/schemas/{resource_id}/children")
async def children(resource_id: str, request: Request, response: Response,
                   table: Optional[str] = Query(default=None, max_length=1024),
                   path: str = Query(default="", max_length=4096), after: str = Query(default="", max_length=4096),
                   limit: int = Query(default=200, ge=1, le=500), db=Depends(get_db),
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
    catalog = '"' + sql_access.catalog_name(source).replace('"', '""') + '"'
    if table is None:
        statement = (f"SELECT table_name, table_type FROM {catalog}.information_schema.tables "
                     f"WHERE table_schema = ? AND table_name > ? ORDER BY table_name LIMIT {limit + 1}")
        parameters = [schema.name, after]
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
        items = [{"name": row[0], "kind": "view" if row[1] == "VIEW" else
                  "materialized_view" if row[1] == "MATERIALIZED VIEW" else "table"} for row in rows]
        cursor = rows[-1][0] if rows and len(result["rows"]) > limit else None
    else:
        items = [{"name": row[0], "kind": "column", "data_type": row[1], "nullable": row[2] == "YES",
                  "ordinal_position": row[3]} for row in rows]
        cursor = str(rows[-1][3]) if rows and len(result["rows"]) > limit else None
    from ..catalog import index_children
    await run_in_threadpool(transaction_call, db, index_children, schema, items, table, path)
    return {"children": items, "next_cursor": cursor}
