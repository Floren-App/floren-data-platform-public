"""Authenticated Trino client protocol, paginated SQL requests, and internal policy callbacks."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .. import sql_access as sql
from ..auth import mint_token, read_token
from ..config import get_settings
from ..db import get_db
from ..models import SqlQuery, utcnow
from .common import audit

router = APIRouter(tags=["shared SQL"])


def transaction_call(db, function, *args):
    """Keep synchronous database waits off the event loop and release locks before yielding."""
    try:
        result = function(db, *args)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def enabled():
    if get_settings().query_executor != "trino":
        raise HTTPException(409, "shared Trino execution is not enabled")
    if not get_settings().trino_signing_key and not get_settings().trino_jwt_private_key:
        raise HTTPException(503, "Trino infrastructure authentication is not configured")


def admit(db, who, catalog, layer=""):
    """The engine follows the data: a catalog's source names its layer."""
    source = sql.catalog_sources(db).get(catalog) if catalog else None
    from ..layers import canonical
    requested = canonical(db, source.config.get("layer_id")) if source and (source.config or {}).get("layer_id") else layer
    if source and layer and canonical(db, layer) != requested:
        raise HTTPException(422, "the catalog belongs to a different layer")
    return sql.admit(db, who, catalog, sql.engine_layer(db, who, requested))


def client_headers(request):
    allowed = {"x-trino-catalog", "x-trino-schema", "x-trino-time-zone", "x-trino-language",
               "x-trino-prepared-statement", "x-trino-client-capabilities"}
    headers = {k: v for k, v in request.headers.items() if k in allowed}
    # Result spooling and client-selected storage endpoints are deliberately unavailable.
    headers["X-Trino-Query-Data-Encoding"] = "json"
    transaction = request.headers.get("x-trino-transaction-id", "NONE")
    if transaction != "NONE":
        raise HTTPException(422, "shared SQL uses autocommit; explicit transactions are not supported")
    headers["X-Trino-Transaction-Id"] = "NONE"
    session = request.headers.get("x-trino-session", "")
    if session:
        for part in session.split(","):
            key = part.split("=", 1)[0].strip()
            if key not in sql.SAFE_SYSTEM_PROPERTIES:
                raise HTTPException(403, "unapproved SQL session property")
        headers["X-Trino-Session"] = session
    return headers


def root_url(request):
    # Trusted deployment URL for public requests, configured internal URL for executions.
    # Forwarded Host/Proto cannot turn a result into an attacker-controlled URL.
    host = urlsplit(str(request.base_url)).netloc
    public = get_settings().public_url.rstrip("/")
    internal = get_settings().internal_url.rstrip("/")
    return public if host == urlsplit(public).netloc else internal


def page_uri(request, query, uri):
    token = mint_token("sql-page", query.principal_id, get_settings().sql_query_ttl_seconds,
                       q=query.id, path=sql.engine_path(uri))
    return root_url(request) + "/v1/sql/pages/" + token


async def dispatch(db, query, method, path, **kwargs):
    try:
        return await sql.engine_request(method, path, query=query, **kwargs)
    except HTTPException:
        query.state = "FAILED"
        audit(db, query.actor, "sql.unavailable", query.id, engine_query_id=query.engine_query_id)
        db.commit()
        raise


def finish_response(db, query, response, request):
    previous_state = query.state
    if response.status_code == 204:
        if query.state != "CANCELED":
            query.state = "CANCELED"
            audit(db, query.actor, "sql.cancel", query.id, engine_query_id=query.engine_query_id,
                  principal_id=query.principal_id, run=query.run_id, workload=query.workload_id)
            db.commit()
        return Response(status_code=204)
    if response.status_code != 200:
        query.state = "FAILED"
        audit(db, query.actor, "sql.failed", query.id, engine_status=response.status_code)
        db.commit()
        raise HTTPException(502, "SQL engine rejected the request")
    try:
        body = response.json()
    except ValueError:
        raise HTTPException(502, "invalid SQL engine response") from None
    query.engine_query_id = body.get("id", query.engine_query_id)
    query.state = ("FAILED" if body.get("error") else
                   body.get("stats", {}).get("state", "RUNNING"))
    if "nextUri" not in body and not body.get("error"):
        query.state = "FINISHED"
    for key in ("nextUri", "partialCancelUri"):
        if body.get(key):
            body[key] = page_uri(request, query, body[key])
    body["infoUri"] = root_url(request) + "/v1/sql/queries/" + query.id
    if body.get("error"):
        body["error"].pop("failureInfo", None)
    if query.state in {"FINISHED", "FAILED"} and previous_state != query.state:
        audit(db, query.actor, "sql." + query.state.lower(), query.id,
              engine_query_id=query.engine_query_id, principal_id=query.principal_id,
              initiator_id=query.initiator_id, run=query.run_id, workload=query.workload_id,
              error_name=(body.get("error") or {}).get("errorName"))
        if query.state == "FINISHED" and query.workload_id:
            # OPA callbacks write table observations in a separate transaction.
            db.refresh(query, ["lineage"])
            from ..catalog import successful_query
            successful_query(db, query)
    db.commit()
    headers = {k: v for k, v in response.headers.items() if k.lower() in {
        "x-trino-set-catalog", "x-trino-set-schema", "x-trino-added-prepare", "x-trino-deallocated-prepare",
        "x-trino-set-session", "x-trino-clear-session"}}
    return JSONResponse(body, headers=headers)


@router.post("/v1/statement")
async def statement(request: Request, db=Depends(get_db), authorization: Optional[str] = Header(default=None),
                    x_fdp_viewer: str = Header(default=""), x_fdp_layer: str = Header(default="")):
    enabled()
    who = await run_in_threadpool(transaction_call, db, sql.actor, authorization, x_fdp_viewer)
    raw = await request.body()
    if not raw or len(raw) > get_settings().sql_max_statement_bytes:
        raise HTTPException(413, "SQL statement is empty or exceeds the configured limit")
    headers = client_headers(request)
    try:
        statement_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "SQL must be UTF-8") from None
    from ..metadata_sql import execute as metadata_execute
    management = await run_in_threadpool(transaction_call, db, metadata_execute, who, statement_text,
        request.headers.get("x-trino-catalog", ""), request.headers.get("x-trino-schema", ""),
        request.headers.get("x-trino-prepared-statement", ""))
    if management is not None:
        management["infoUri"] = root_url(request) + "/v1/sql/queries/" + management["id"].removeprefix("fdp_management_")
        return JSONResponse(management)
    sql.validate_statement(statement_text, request.headers.get("x-trino-prepared-statement", ""))
    query = await run_in_threadpool(transaction_call, db, admit, who, request.headers.get("x-trino-catalog", ""),
                                    x_fdp_layer)
    try:
        sql.authorize_initial_creates(query, statement_text, request.headers.get('x-trino-prepared-statement', ''),
                                     request.headers.get('x-trino-catalog', ''), request.headers.get('x-trino-schema', ''))
    except HTTPException:
        query.state = 'FAILED'
        db.commit()
        raise
    headers.update(sql.engine_headers(query))
    audit(db, query.actor, "sql.submit", query.id, principal_id=query.principal_id,
          initiator_id=query.initiator_id, run=query.run_id, workload=query.workload_id,
          catalogs=list(query.scope))
    db.commit()
    response = await dispatch(db, query, "POST", "/v1/statement", headers=headers, content=raw)
    return finish_response(db, query, response, request)


def owned_query(db, query_id, who):
    query = db.get(SqlQuery, query_id)
    if (not query or query.principal_id != who.user.principal_id or query.binding != who.binding):
        raise HTTPException(404, "SQL request not found")
    if query.expires_at.replace(tzinfo=utcnow().tzinfo) <= utcnow():
        raise HTTPException(410, "SQL request expired")
    return query


@router.api_route("/v1/sql/pages/{token}", methods=["GET", "DELETE"])
async def page(token: str, request: Request, db=Depends(get_db), authorization: Optional[str] = Header(default=None),
               x_fdp_viewer: str = Header(default="")):
    enabled()
    claims = read_token(token, "sql-page")
    who = await run_in_threadpool(transaction_call, db, sql.actor, authorization, x_fdp_viewer)
    query = await run_in_threadpool(transaction_call, db, owned_query, claims.get("q"), who)
    if claims.get("sub") != query.principal_id:
        raise HTTPException(403, "invalid continuation identity")
    path = claims.get("path", "")
    if not path.startswith("/v1/statement/"):
        raise HTTPException(403, "invalid continuation path")
    # resolve_user takes a directory row lock. Release it before awaiting Trino;
    # another viewer request must never block this event loop behind that lock.
    db.commit()
    response = await dispatch(db, query, request.method, path, headers=sql.engine_headers(query))
    return finish_response(db, query, response, request)


@router.get("/v1/sql/queries/{query_id}")
def query_status(query_id: str, db=Depends(get_db), authorization: Optional[str] = Header(default=None),
                 x_fdp_viewer: str = Header(default="")):
    enabled()
    query = owned_query(db, query_id, sql.actor(db, authorization, x_fdp_viewer))
    db.commit()
    return {"id": query.id, "engine_query_id": query.engine_query_id, "state": query.state,
            "created_at": query.created_at, "principal_id": query.principal_id}


@router.get("/v1/info")
def info(db=Depends(get_db), authorization: Optional[str] = Header(default=None)):
    enabled()
    sql.actor(db, authorization)
    db.commit()
    return {"nodeVersion": {"version": "484"}, "environment": "fdp", "coordinator": True, "starting": False}


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    sql: str = Field(min_length=1, max_length=1000000)
    parameters: List[Any] = Field(default_factory=list)
    max_rows: int = Field(default=1000, ge=1, le=10000)


def literal(value):
    import math
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise HTTPException(422, "nonfinite SQL parameter")
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise HTTPException(422, "SQL parameters must be scalar JSON values")


@router.post("/v1/sql/query")
async def query_request(body: QueryIn, request: Request, db=Depends(get_db),
                        authorization: Optional[str] = Header(default=None),
                        x_fdp_viewer: str = Header(default="")):
    enabled()
    who = await run_in_threadpool(transaction_call, db, sql.actor, authorization, x_fdp_viewer)
    return await query_for_actor(body, request, db, who)


async def query_for_actor(body, request, db, who):
    """Internal caller supplies a server-authenticated, optionally narrowed actor."""
    # Validate parameter types before creating any durable admission record.
    bound = [literal(v) for v in body.parameters]
    from ..models import DataSource
    source = db.scalar(select(DataSource).where(DataSource.name == body.source))
    if not source or source.kind not in sql.SQL_KINDS:
        raise HTTPException(404, "SQL source not available")
    catalog = sql.catalog_name(source)
    statement = body.sql
    if body.parameters:
        statement = "EXECUTE IMMEDIATE " + literal(body.sql) + " USING " + ", ".join(bound)
    from ..metadata_sql import execute as metadata_execute
    management = await run_in_threadpool(transaction_call, db, metadata_execute, who, statement, catalog)
    if management is not None:
        return {"columns": ["rows"], "rows": management["data"], "truncated": False,
                "rowcount": management["updateCount"], "query_id": management["id"].removeprefix("fdp_management_"),
                "engine_query_id": management["id"]}
    sql.validate_statement(body.sql)
    query = await run_in_threadpool(transaction_call, db, admit, who, catalog)
    default_schema = next(iter(query.scope[catalog]['schemas']))
    try:
        sql.authorize_initial_creates(query, body.sql, catalog=catalog, schema=default_schema)
    except HTTPException:
        query.state = 'FAILED'
        db.commit()
        raise
    headers = sql.engine_headers(query)
    headers.update({"X-Trino-Catalog": catalog, "X-Trino-Schema": default_schema,
                    "X-Trino-Transaction-Id": "NONE"})
    audit(db, query.actor, "sql.submit", query.id, principal_id=query.principal_id, initiator_id=query.initiator_id,
          run=query.run_id, workload=query.workload_id, catalogs=list(query.scope))
    db.commit()
    response = await dispatch(db, query, "POST", "/v1/statement", headers=headers, content=statement.encode())
    rows, columns, next_path = [], [], None
    import asyncio
    import json
    import time
    deadline = time.monotonic() + 30
    try:
        while True:
            # Keep the same response sanitation/audit path as standard Trino clients.
            sanitized = finish_response(db, query, response, request)
            data = json.loads(sanitized.body)
            original = response.json()
            next_path = sql.engine_path(original["nextUri"]) if original.get("nextUri") else None
            if data.get("error"):
                raise HTTPException(422, {"message": data["error"].get("message", "SQL request denied or unsupported"),
                                          "error_name": data["error"].get("errorName"), "query_id": query.id})
            columns = data.get("columns", columns)
            rows.extend(data.get("data") or [])
            # Use engine URIs, never feed the gateway's rewritten URI back into Trino.
            if len(rows) > body.max_rows or not next_path:
                break
            response = await asyncio.wait_for(dispatch(db, query, "GET", next_path, headers=headers),
                                              timeout=max(0.01, deadline - time.monotonic()))
    except (TimeoutError, asyncio.TimeoutError):
        raise HTTPException(504, "interactive SQL exceeded 30 seconds; use connect() for long queries") from None
    finally:
        if next_path:
            try:
                await sql.engine_request("DELETE", next_path, headers=headers, query=query)
                query.state = "CANCELED"
            except HTTPException:
                query.state = "FAILED"
            db.commit()
    return {"columns": [c["name"] for c in columns], "rows": rows[:body.max_rows],
            "truncated": len(rows) > body.max_rows, "rowcount": response.json().get("updateCount", len(rows)),
            "query_id": query.id, "engine_query_id": query.engine_query_id}


@router.get("/internal/trino/jwks", include_in_schema=False)
def trino_jwks():
    """Public verification keys for layer Trino coordinators; never a secret."""
    return sql.layer_jwks()


@router.post("/internal/trino/allow", include_in_schema=False)
def allow(body: Dict[str, Any], db=Depends(get_db)):
    return policy_allow(body, db)


@router.post("/internal/trino/layers/{layer}/allow", include_in_schema=False)
def layer_allow(layer: str, body: Dict[str, Any], db=Depends(get_db)):
    from ..trino_scaler import monitor_read
    if monitor_read(body):
        return {"result": True}
    return policy_allow(body, db, layer)


def policy_allow(body, db, layer=""):
    try:
        payload = body.get("input") or {}
        query = sql.policy_query(db, payload, layer)
        action = payload.get("action") or {}
        permitted = sql.policy_allow(query, action)
        operation = action.get("operation")
        table = (action.get("resource") or {}).get("table") or {}
        relation = "reads" if operation == "SelectFromColumns" else "writes" if operation in {"InsertIntoTable", "UpdateTableColumns", "DeleteFromTable"} else "creates" if operation == "CreateTable" else None
        if permitted and query.workload_id and relation and table.get("tableName") and table.get("schemaName") not in ("information_schema", "metadata"):
            query = db.scalar(select(SqlQuery).where(SqlQuery.id == query.id).with_for_update().execution_options(populate_existing=True))
            key = "/".join(str(table.get(k, "")) for k in ("catalogName", "schemaName", "tableName"))
            observations = dict(query.lineage or {})
            previous = observations.get(key, {})
            observations[key] = {"catalog": table["catalogName"], "schema": table["schemaName"], "table": table["tableName"],
                                 "relations": sorted(set(previous.get("relations", [])) | {relation}
                    | ({"writes"} if relation == "creates" and [table["schemaName"], table["tableName"]]
                       in query.scope.get(table["catalogName"], {}).get("lineage_materializations", []) else set()))}
            query.lineage = observations
            db.commit()
        return {"result": permitted}
    except (HTTPException, TypeError, AttributeError, KeyError):
        return {"result": False}


@router.post("/internal/trino/batch", include_in_schema=False)
def batch(body: Dict[str, Any], db=Depends(get_db)):
    return policy_batch(body, db)


@router.post("/internal/trino/layers/{layer}/batch", include_in_schema=False)
def layer_batch(layer: str, body: Dict[str, Any], db=Depends(get_db)):
    return policy_batch(body, db, layer)


def policy_batch(body, db, layer=""):
    try:
        payload = body.get("input") or {}
        query = sql.policy_query(db, payload, layer)
        action = payload.get("action") or {}
        items = action.get("filterResources") or []
        if action.get("operation") == "FilterColumns" and len(items) == 1:
            table = items[0].get("table") or {}
            if sql.policy_allow(query, {**action, "resource": items[0]}):
                return {"result": list(range(len(table.get("columns", []))))}
            return {"result": []}
        return {"result": [i for i, item in enumerate(items)
                           if sql.policy_allow(query, {**action, "resource": item})]}
    except (HTTPException, TypeError, AttributeError, KeyError):
        return {"result": []}


@router.post("/internal/trino/row-filters", include_in_schema=False)
def row_filters(body: Dict[str, Any], db=Depends(get_db)):
    return policy_row_filters(body, db)


@router.post("/internal/trino/layers/{layer}/row-filters", include_in_schema=False)
def layer_row_filters(layer: str, body: Dict[str, Any], db=Depends(get_db)):
    return policy_row_filters(body, db, layer)


def policy_row_filters(body, db, layer=""):
    from ..data_policies import row_filters as table_filters
    payload = body.get("input") or {}
    query = sql.policy_query(db, payload, layer)
    table = (payload.get("action", {}).get("resource") or {}).get("table") or {}
    return {"result": sql.metadata_filters(query, table) + table_filters(query, table)}


@router.post("/internal/trino/batch-column-masks", include_in_schema=False)
def column_masks(body: Dict[str, Any], db=Depends(get_db)):
    return policy_column_masks(body, db)


@router.post("/internal/trino/layers/{layer}/batch-column-masks", include_in_schema=False)
def layer_column_masks(layer: str, body: Dict[str, Any], db=Depends(get_db)):
    return policy_column_masks(body, db, layer)


def policy_column_masks(body, db, layer=""):
    """Masks for a batch of columns. Errors propagate: Trino then fails the query instead of reading unmasked values."""
    from ..data_policies import column_masks as masks
    payload = body.get("input") or {}
    query = sql.policy_query(db, payload, layer)
    return {"result": masks(query, (payload.get("action") or {}).get("filterResources") or [])}
