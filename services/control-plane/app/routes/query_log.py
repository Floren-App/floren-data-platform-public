"""The query log: Trino's completed-query events in, and the log for administrators and people's own queries."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import query_log
from ..auth import User, require_admin
from ..db import get_db
from ..deps import get_actor, get_orchestrator
from ..models import QueryLog
from ..orchestrator import Orchestrator
from .common import audit

router = APIRouter(tags=["observability"])


@router.post("/internal/trino/layers/{layer_id}/events", include_in_schema=False)
async def trino_events(layer_id: str, request: Request, authorization: str = Header(default=""),
                       db: Session = Depends(get_db), orch: Orchestrator = Depends(get_orchestrator)):
    """Trino's HTTP event listener; only with the layer's events token."""
    if not query_log.authorized(orch, layer_id, authorization):
        raise HTTPException(401, "layer events token required")
    body = await request.json()
    for event in body if isinstance(body, list) else [body]:
        if isinstance(event, dict):
            query_log.record(db, layer_id, event)
    db.commit()
    return Response(status_code=204)


def row_out(r: QueryLog) -> dict:
    return {"id": r.id, "layer_id": r.layer_id, "query_id": r.query_id, "actor": r.actor, "principal_id": r.principal_id,
            "run_id": r.run_id, "workload_id": r.workload_id, "workspace_id": r.workspace_id, "trino_user": r.trino_user,
            "source": r.source, "query_type": r.query_type, "state": r.state, "error": r.error, "sql_text": r.sql_text,
            "tables": r.tables or [], "output_table": r.output_table, "rows": r.rows, "bytes": r.bytes,
            "cpu_ms": r.cpu_ms, "wall_ms": r.wall_ms, "queued_ms": r.queued_ms, "created_at": r.created_at, "ended_at": r.ended_at}


@router.get("/v1/query-log")
def list_queries(actor: str = "", layer_id: str = "", state: str = "", table: str = "", before: Optional[datetime] = None,
                 limit: int = 100, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Newest first. Administrators see every query; everyone else the queries they ran or started."""
    query = select(QueryLog).order_by(QueryLog.created_at.desc()).limit(max(1, min(limit, 500)))
    if not (user.is_admin and not user.execution_context):
        mine = user.principal_id or "-"
        query = query.where(or_(QueryLog.principal_id == mine, QueryLog.initiator_id == mine))
    if actor:
        query = query.where(QueryLog.actor.ilike(f"%{actor}%"))
    if layer_id:
        query = query.where(QueryLog.layer_id == layer_id)
    if state:
        query = query.where(QueryLog.state == state.upper())
    if before:
        query = query.where(QueryLog.created_at < before)
    rows = list(db.scalars(query))
    if table:
        rows = [r for r in rows if any(table.lower() in t.lower() for t in (r.tables or []) + [r.output_table])]
    return [row_out(r) for r in rows]


class QueryLogSettingsIn(BaseModel):
    retention_days: int = Field(ge=1, le=3650, description="Rows older than this are removed")
    record_sql: bool = Field(default=False, description="Keep each query's SQL text (it can contain literal values)")


@router.get("/v1/admin/settings/query-log")
def query_log_settings(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return query_log.settings(db)


@router.put("/v1/admin/settings/query-log")
def set_query_log_settings(body: QueryLogSettingsIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    result = query_log.save_settings(db, body.retention_days, body.record_sql)
    audit(db, admin.username, "settings.query_log", query_log.KEY, **body.model_dump())
    db.commit()
    return result


@router.delete("/v1/admin/settings/query-log", status_code=204)
def reset_query_log_settings(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Back to the defaults: 90 days, no SQL text."""
    from ..models import PlatformSetting
    row = db.get(PlatformSetting, query_log.KEY)
    if row:
        db.delete(row)
    audit(db, admin.username, "settings.query_log.reset", query_log.KEY)
    db.commit()
