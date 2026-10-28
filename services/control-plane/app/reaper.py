"""Auto-suspend: the 'warehouse' behaviour. Idle workspaces and apps stop; finished runs are recorded."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from datetime import datetime, timedelta

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from .config import Settings
from .models import Run, Workspace, SqlQuery, utcnow
from .orchestrator import Orchestrator

log = logging.getLogger("fdp.reaper")
FINISHED_RETENTION = timedelta(hours=1)  # keep finished containers around for logs


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


def refresh_run(db: Session, orch: Orchestrator, run: Run, now: Optional[datetime] = None) -> None:
    """Brings an active run's status up to date with its execution (also used while an API call waits)."""
    now = now or utcnow()
    if run.status not in ("pending", "running"):
        return
    state = orch.status(run.execution_ref) if run.execution_ref else "missing"
    if state in ("pending", "running"):
        # A container the cluster cannot place yet is not running; say why it waits.
        reason = getattr(orch, "waiting_reason", lambda ref: None)(run.execution_ref) if state == "pending" else None
        run.status, run.waiting_reason = state, reason
        if reason and any(code in reason for code in ("ErrImageNeverPull", "InvalidImageName")):
            orch.stop(run.execution_ref)  # it will never start: fail now and keep the reason
            run.status, run.finished_at, run.execution_ref = "failed", now, None
            return
    if state in ("succeeded", "failed"):
        run.status, run.finished_at, run.waiting_reason = state, now, None
    elif state == "missing":
        run.status, run.finished_at, run.waiting_reason = "failed", now, None
    elif run.kind in ("app", "airflow") and now - _aware(run.last_activity_at) > timedelta(seconds=run.pool.auto_suspend_seconds):
        log.info("suspending idle app %s", run.id)
        orch.stop(run.execution_ref)
        run.status, run.finished_at, run.execution_ref, run.upstream_url = "stopped", now, None, None
    if run.status == "succeeded":
        from .catalog import finished_run
        finished_run(db, run)


def reap_once(db: Session, orch: Orchestrator, s: Settings) -> None:
    now = utcnow()
    idle_ws = timedelta(seconds=s.workspace_idle_timeout_seconds)
    from .workspace_lifecycle import refresh_workspace
    for ws in db.scalars(select(Workspace).where(Workspace.status.in_(("starting","running"))).with_for_update(skip_locked=True)):
        refresh_workspace(db,orch,ws,s,now)
        if ws.status == "running" and now - _aware(ws.last_activity_at) > idle_ws:
            log.info("suspending idle workspace %s", ws.id)
            if ws.execution_ref:
                orch.stop(ws.execution_ref)
            ws.status, ws.execution_ref, ws.upstream_url = "stopped", None, None

    for run in db.scalars(select(Run).where(Run.status.in_(("pending", "running")))):
        refresh_run(db, orch, run, now)

    for run in db.scalars(select(Run).where(Run.finished_at.is_not(None), Run.execution_ref.is_not(None))):
        if now - _aware(run.finished_at) > FINISHED_RETENTION:
            orch.stop(run.execution_ref)
            run.execution_ref = None
    db.commit()
    # Group templates follow memberships (new members get their workspaces, leavers lose theirs).
    from .workspace_templates import reconcile as reconcile_templates
    reconcile_templates(db, orch)
    db.commit()
    # Freed capacity goes to queued jobs, deploys and workspaces, oldest first.
    from .capacity import dispatch
    for item in dispatch(db, orch):
        log.info("started from the queue: %s", item)
    # Admission records are operational state; the append-only audit log is separate.
    db.execute(delete(SqlQuery).where(SqlQuery.expires_at < now - timedelta(days=7)))
    db.commit()
    from .data_access import cleanup_leases
    from .deps import get_secret_store
    cleanup_leases(db, get_secret_store())


async def reaper_loop(session_factory, orch: Orchestrator, s: Settings) -> None:
    while True:
        await asyncio.sleep(s.reaper_interval_seconds)
        try:
            db = session_factory()
            try:
                await asyncio.to_thread(reap_once, db, orch, s)
            finally:
                db.close()
        except Exception:  # keep the loop alive; one bad tick must not stop auto-suspend
            log.exception("reaper tick failed")
