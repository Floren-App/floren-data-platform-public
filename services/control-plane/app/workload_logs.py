"""The central workload log: platform entries recorded as things happen, program entries from fdp.log.

Run lifecycle (started, succeeded, failed, stopped) and workload deploy events are written from one session
hook, whichever code path changed them, so nothing has to remember to log."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import delete, event, inspect
from sqlalchemy.orm import Session

from .models import Run, Workload, WorkloadEvent, WorkloadLog, new_id, utcnow

log = logging.getLogger("fdp.workload_logs")
LEVELS = ("debug", "info", "warning", "error")
MAX_MESSAGE = 4000
MAX_FIELDS = 4096
RETENTION = timedelta(days=30)

# Workload events (also shown in a workload's deploy history) and how they read in the log.
_WORKLOAD_EVENTS = {"deployed": "info", "update_found": "info", "queued": "warning", "restarting": "warning",
                    "deploy_failed": "error", "sync_failed": "error"}


def clean_fields(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Small, JSON-safe structured context; anything larger is cut, never refused."""
    if not fields:
        return {}
    try:
        text = json.dumps(fields, default=str)
    except (TypeError, ValueError):
        return {"fields": "unserializable"}
    if len(text) > MAX_FIELDS:
        return {"truncated": text[:MAX_FIELDS]}
    return json.loads(text)


def _layer(db: Session, run: Optional[Run], workload: Optional[Workload]) -> Optional[str]:
    from .layers import info, layer_of
    from .rbac import workload_resource
    with db.no_autoflush:
        try:
            if run is not None:
                return layer_of(db, run.environment_id)
            return info(db, workload_resource(db, workload))["layer_id"] if workload is not None else None
        except Exception:  # a log line never breaks the change it describes
            return None


def entry(db: Session, *, level: str, message: str, event_name: str = "", source: str = "platform",
          run: Optional[Run] = None, workload: Optional[Workload] = None, workspace_id: Optional[str] = None,
          commit_sha: Optional[str] = None, fields: Optional[Dict[str, Any]] = None, actor: str = "",
          layer_id: Optional[str] = None, at=None) -> WorkloadLog:
    row = WorkloadLog(
        at=at or utcnow(), level=level if level in LEVELS else "info", source=source, event=event_name[:48],
        message=(message or "")[:MAX_MESSAGE], fields=clean_fields(fields),
        workload_id=(run.workload_id if run else None) or (workload.id if workload else None),
        run_id=run.id if run else None,
        workspace_id=workspace_id or (run.workspace_id if run else None),
        commit_sha=commit_sha or (run.commit_sha if run else None) or (workload.deployed_sha if workload else None),
        layer_id=layer_id or _layer(db, run, workload),
        actor=(actor or (run.initiated_by or run.owner if run else "") or "")[:256])
    db.add(row)
    return row


def _duration(run: Run) -> str:
    if not run.finished_at or not run.created_at:
        return ""
    start = run.created_at if run.created_at.tzinfo else run.created_at.replace(tzinfo=run.finished_at.tzinfo)
    seconds = int((run.finished_at - start).total_seconds())
    return f" after {seconds // 60}m {seconds % 60}s" if seconds >= 60 else f" after {max(seconds, 0)}s"


def _what(run: Run) -> str:
    return run.program or run.entrypoint


def _run_changed(db: Session, run: Run, before: Optional[str]) -> None:
    status = run.status
    if status == before:
        return
    common = dict(run=run, fields={"warehouse": run.pool.name if run.pool else None, "kind": run.kind})
    if status in ("pending", "running") and before in (None, "queued"):
        entry(db, level="info", event_name="run.started", message=f"{_what(run)} started", **common)
    elif status == "queued" and before is None:
        entry(db, level="info", event_name="run.queued", message=f"{_what(run)} is waiting for capacity", **common)
    elif status == "succeeded":
        entry(db, level="info", event_name="run.succeeded", message=f"{_what(run)} succeeded{_duration(run)}", **common)
    elif status == "failed":
        reason = f": {run.waiting_reason}" if run.waiting_reason else ""
        entry(db, level="error", event_name="run.failed", message=f"{_what(run)} failed{_duration(run)}{reason}", **common)
    elif status == "stopped":
        entry(db, level="warning", event_name="run.stopped", message=f"{_what(run)} was stopped{_duration(run)}", **common)


@event.listens_for(Session, "before_flush")
def _record(session: Session, _context, _instances) -> None:
    try:
        for obj in list(session.new) + list(session.dirty):
            if isinstance(obj, Run):
                history = inspect(obj).attrs.status.history
                if obj in session.new:
                    if obj.id is None:  # the column default would only assign it during this flush
                        obj.id = new_id()
                    _run_changed(session, obj, None)
                elif history.has_changes():
                    _run_changed(session, obj, history.deleted[0] if history.deleted else None)
            elif isinstance(obj, WorkloadEvent) and obj in session.new and obj.kind in _WORKLOAD_EVENTS:
                workload = obj.workload or (session.get(Workload, obj.workload_id) if obj.workload_id else None)
                if workload is not None:
                    entry(session, level=_WORKLOAD_EVENTS[obj.kind], event_name="workload." + obj.kind, message=obj.message,
                          workload=workload, commit_sha=obj.sha, actor="platform")
    except Exception:  # never fail the change being logged
        log.exception("workload log entry")


def prune(db: Session) -> int:
    return db.execute(delete(WorkloadLog).where(WorkloadLog.at < utcnow() - RETENTION)).rowcount or 0


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
                log.info("workload log: removed %d entries past retention", removed)
        except Exception:
            log.exception("workload log retention")
        await asyncio.sleep(interval)
