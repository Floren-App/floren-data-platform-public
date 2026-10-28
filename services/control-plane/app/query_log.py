"""The query log: every query that runs on a layer's Trino, with who or what ran it.

Each layer's coordinator posts a completed-query event (Trino's HTTP event listener) to the control plane with
the layer's events token. The gateway passed its admission record's id as Trino's trace token, so the event is
joined with it: the person or service identity, the run, workload or workspace it ran in. SQL text is kept
only when administrators turn it on, because literal values in queries can be sensitive. Rows older than the
retention are removed by the leader.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import PlatformSetting, QueryLog, SqlQuery, utcnow

log = logging.getLogger(__name__)
KEY = "query_log"
DEFAULTS = {"retention_days": 90, "record_sql": False}
MAX_SQL = 20000
completed_total: Dict[tuple, int] = {}   # (layer, state) -> count, for this replica's metrics


def settings(db: Session) -> dict:
    row = db.get(PlatformSetting, KEY)
    return {**DEFAULTS, **((row.value or {}) if row else {})}


def save_settings(db: Session, retention_days: int, record_sql: bool) -> dict:
    row = db.get(PlatformSetting, KEY) or PlatformSetting(key=KEY)
    row.value = {"retention_days": retention_days, "record_sql": record_sql}
    db.add(row)
    return settings(db)


# --- tokens -------------------------------------------------------------------------------------------------

_tokens: Dict[str, tuple] = {}


def layer_token(orch, layer_id: str) -> Optional[str]:
    """The layer's events token (from its namespace), cached for a minute."""
    cached = _tokens.get(layer_id)
    if cached and time.monotonic() - cached[1] < 60:
        return cached[0]
    from .config import get_settings
    from .layer_infrastructure import namespace_for
    from .layer_manifests import EVENTS_SECRET
    reader = getattr(orch, "read_layer_secret", None)
    token = reader(namespace_for(layer_id, get_settings().layer_namespace_prefix or "fdp-layer"), EVENTS_SECRET, "events-token") if reader else None
    _tokens[layer_id] = (token, time.monotonic())
    return token


def authorized(orch, layer_id: str, authorization: str) -> bool:
    token = layer_token(orch, layer_id)
    return bool(token) and hmac.compare_digest(authorization or "", "Bearer " + token)


# --- parsing Trino's event --------------------------------------------------------------------------------

_UNITS = {"ns": 1e-6, "us": 1e-3, "ms": 1.0, "s": 1000.0, "m": 60000.0, "h": 3600000.0, "d": 86400000.0}


def millis(value: Any) -> int:
    """Durations as Trino may send them: seconds as a number, ISO-8601 (PT1.5S) or Airlift text (1.50s)."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value * 1000)
    text = str(value).strip()
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", text)
    if m and text not in ("P", "PT"):
        days, hours, minutes, seconds = (float(x) if x else 0.0 for x in m.groups())
        return int(((days * 24 + hours) * 60 + minutes) * 60000 + seconds * 1000)
    m = re.fullmatch(r"([\d.]+)\s*(ns|us|ms|s|m|h|d)", text)
    return int(float(m.group(1)) * _UNITS[m.group(2)]) if m else 0


def timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _table(io: dict) -> str:
    parts = [io.get("catalogName") or io.get("catalog"), io.get("schema"), io.get("table")]
    return ".".join(p for p in parts if p)


def record(db: Session, layer_id: str, event: dict) -> Optional[QueryLog]:
    """Stores one completed-query event (idempotent per Trino query id)."""
    meta, stats, context = event.get("metadata") or {}, event.get("statistics") or {}, event.get("context") or {}
    query_id = meta.get("queryId")
    if not query_id:
        return None
    row = db.scalar(select(QueryLog).where(QueryLog.layer_id == layer_id, QueryLog.query_id == query_id))
    if row is not None:
        return row
    io = event.get("ioMetadata") or {}
    failure = event.get("failureInfo") or {}
    error = failure.get("errorCode") or {}
    trace = context.get("traceToken") or ""
    admitted = db.get(SqlQuery, trace) if trace else None
    row = QueryLog(layer_id=layer_id, query_id=query_id[:128], trino_user=(context.get("user") or "")[:256],
                   source=(context.get("source") or "")[:128], query_type=(context.get("queryType") or "")[:32],
                   state=(meta.get("queryState") or "")[:16],
                   error=" ".join(x for x in [error.get("name"), failure.get("failureMessage")] if x)[:600],
                   tables=sorted({_table(i) for i in io.get("inputs") or [] if _table(i)})[:200],
                   output_table=_table(io.get("output") or {})[:400],
                   rows=int(stats.get("outputRows") or stats.get("totalRows") or 0),
                   bytes=int(stats.get("totalBytes") or stats.get("physicalInputBytes") or 0),
                   cpu_ms=millis(stats.get("cpuTime")), wall_ms=millis(stats.get("wallTime")),
                   queued_ms=millis(stats.get("queuedTime")),
                   created_at=timestamp(event.get("createTime")) or utcnow(), ended_at=timestamp(event.get("endTime")))
    if admitted is not None:
        row.sql_query_id, row.principal_id, row.actor = admitted.id, admitted.principal_id, admitted.actor
        row.initiator_id, row.run_id, row.workload_id = admitted.initiator_id, admitted.run_id, admitted.workload_id
        row.workspace_id = admitted.workspace_id or ""
    else:
        row.actor = row.trino_user
    if settings(db)["record_sql"]:
        row.sql_text = (meta.get("query") or "")[:MAX_SQL]
    db.add(row)
    completed_total[(layer_id, row.state)] = completed_total.get((layer_id, row.state), 0) + 1
    return row


def prune(db: Session) -> int:
    cutoff = utcnow() - timedelta(days=settings(db)["retention_days"])
    return db.execute(delete(QueryLog).where(QueryLog.created_at < cutoff)).rowcount or 0


async def retention_loop(session_factory, interval: float = 3600) -> None:
    while True:
        try:
            def once():
                with session_factory() as db:
                    removed = prune(db)
                    db.commit()
                    return removed
            removed = await asyncio.to_thread(once)
            if removed:
                log.info("query log: removed %d rows past retention", removed)
        except Exception:
            log.exception("query log retention")
        await asyncio.sleep(interval)
