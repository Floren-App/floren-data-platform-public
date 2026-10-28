"""Viewer-bound database requests for shared apps. Never returns viewer credentials to apps."""
from typing import Any, Dict, Optional, Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..auth import User, Execution, get_execution, read_token
from ..db import get_db
from ..deps import get_secret_store
from ..models import DataSource, Workload, CredentialLease, utcnow
from .. import rbac
from ..data_access import credentials, cleanup_leases
from ..secrets import SecretStore
from .common import audit

router = APIRouter(prefix="/v1/delegated", tags=["viewer data access"])

class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    sql: str = Field(min_length=1, max_length=100000)
    parameters: Any = Field(default_factory=list)
    max_rows: int = Field(default=1000, ge=1, le=10000)


@router.post("/query")
async def query(body: QueryIn, request: Request, x_fdp_viewer: Optional[str] = Header(default=None),
          authorization: str = Header(default=""),
          wl: Execution = Depends(get_execution), db: Session = Depends(get_db),
          store: SecretStore = Depends(get_secret_store)):
    from ..config import get_settings
    if get_settings().query_executor == "trino":
        from .sql import QueryIn as SharedQuery, query_request
        if not isinstance(body.parameters, list) and body.parameters:
            raise HTTPException(422, "Trino parameters use a list and positional ? placeholders")
        return await query_request(SharedQuery(source=body.source, sql=body.sql,
            parameters=body.parameters or [], max_rows=body.max_rows), request, db, authorization, x_fdp_viewer or "")
    user, pub, ds, scope = viewer_scope(body.source, x_fdp_viewer, wl, db)
    if ds.kind != "postgres":
        raise HTTPException(422, "delegated SQL currently requires PostgreSQL")
    cred, _ = credentials(db, ds, scope, store, user.username)
    try:
        import psycopg
        with psycopg.connect(host=ds.config.get("host"), port=ds.config.get("port", 5432),
                dbname=ds.config.get("database"), user=cred["username"], password=cred["password"],
                connect_timeout=10, options="-c statement_timeout=30000 -c lock_timeout=5000",
                **({"sslmode": ds.config["sslmode"]} if ds.config.get("sslmode") else {})) as conn:
            with conn.cursor() as cur:
                # Prepared execution accepts one statement; native role is the authorization boundary.
                cur.execute(body.sql, body.parameters or None, prepare=True)
                columns = [c.name for c in cur.description] if cur.description else []
                result = cur.fetchmany(body.max_rows + 1) if columns else []
                response = {"columns": columns, "rows": result[:body.max_rows],
                            "truncated": len(result) > body.max_rows, "rowcount": cur.rowcount}
        audit(db, user.username, "viewer.query", ds.name, workload=pub.id, scope=scope)
        db.commit()
        return response
    except psycopg.Error:
        # Do not leak provisioning information or query parameter contents in errors/audit.
        raise HTTPException(422, "database request failed or was denied by native permissions") from None
    finally:
        lease = db.scalar(select(CredentialLease).where(CredentialLease.username == cred["username"]))
        if lease:
            lease.expires_at = utcnow()
            db.commit()
            cleanup_leases(db, store)


def viewer_scope(source_name, x_fdp_viewer, wl, db):
    if not x_fdp_viewer:
        raise HTTPException(401, "viewer delegation is required")
    viewer = read_token(x_fdp_viewer, "viewer")
    pub = db.get(Workload, wl.workload_id) if wl.workload_id else None
    if (not pub or pub.status != "live" or pub.execution_mode != "caller" or
            viewer.get("pub") != pub.id or wl.username != pub.identity):
        raise HTTPException(403, "viewer delegation does not belong to this running caller-mode service")
    from ..environments import bind
    user = rbac.resolve_user(db, bind(db, User(viewer["sub"], principal_id=viewer.get("pid", "")), rbac.workload_resource(db, pub)))
    rbac.require(db, user, "execute", rbac.workload_resource(db, pub))
    ds = db.scalar(select(DataSource).where(DataSource.name == source_name))
    scope = rbac.data_scope(db, user, ds) if ds else {}
    if not scope:
        raise HTTPException(404, "data source not available")
    return user, pub, ds, scope


class FileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    operation: Literal["read", "write", "delete", "list"]
    path: str = Field(default="", max_length=1024)
    data: str = Field(default="", max_length=7 * 1024 * 1024)
    content_type: str = Field(default="application/octet-stream", max_length=128)


@router.post("/files")
def files(body: FileIn, x_fdp_viewer: Optional[str] = Header(default=None),
          wl: Execution = Depends(get_execution), db: Session = Depends(get_db),
          store: SecretStore = Depends(get_secret_store)):
    import base64
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
    from ..storage_access import prefix_path
    user, pub, ds, scope = viewer_scope(body.source, x_fdp_viewer, wl, db)
    if ds.kind != "s3":
        raise HTTPException(422, "delegated file access requires an S3 source")
    key = prefix_path(ds.config.get("prefix", ""), body.path)
    cred, _ = credentials(db, ds, scope, store, user.username)
    client = boto3.client("s3", endpoint_url=ds.config.get("endpoint_url"), region_name=ds.config.get("region", "us-east-1"),
                         aws_access_key_id=cred["access_key_id"], aws_secret_access_key=cred["secret_access_key"],
                         aws_session_token=cred["session_token"], config=Config(s3={"addressing_style": "path"},
                         connect_timeout=10, read_timeout=15, retries={"max_attempts": 1}))
    bucket = ds.config["bucket"]
    try:
        if body.operation == "read":
            obj = client.get_object(Bucket=bucket, Key=key)
            with obj["Body"] as stream:
                data = stream.read(5 * 1024 * 1024 + 1)
            if len(data) > 5 * 1024 * 1024:
                raise HTTPException(413, "delegated file reads are limited to 5 MB")
            result = {"data": base64.b64encode(data).decode(), "content_type": obj.get("ContentType")}
        elif body.operation == "write":
            try:
                data = base64.b64decode(body.data, validate=True)
            except ValueError:
                raise HTTPException(422, "file data must be base64 encoded") from None
            if len(data) > 5 * 1024 * 1024:
                raise HTTPException(413, "delegated file writes are limited to 5 MB")
            client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=body.content_type)
            result = {"ok": True}
        elif body.operation == "delete":
            client.delete_object(Bucket=bucket, Key=key)
            result = {"ok": True}
        else:
            prefix = key.rstrip("/") + "/" if key else ""
            listing = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1000)
            base_prefix = ds.config.get("prefix", "").strip("/")
            result = {"files": [{"path": o["Key"][len(base_prefix)+1:] if base_prefix else o["Key"],
                                 "size": o["Size"], "modified": o["LastModified"].isoformat()}
                                for o in listing.get("Contents", [])], "truncated": listing.get("IsTruncated", False)}
    except (BotoCoreError, ClientError):
        raise HTTPException(403, "file request failed or was denied by storage permissions") from None
    audit(db, user.username, "viewer.file." + body.operation, ds.name, workload=pub.id, path=body.path)
    db.commit()
    return result
