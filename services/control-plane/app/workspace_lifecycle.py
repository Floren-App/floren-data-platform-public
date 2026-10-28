"""Editor readiness is separate from a running container. Survives control-plane restarts."""
import asyncio
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select

from .models import Workspace, utcnow

log = logging.getLogger(__name__)


def ready_url(ws):
    suffix = f"/ws/{ws.id}/status" if ws.editor == "sql" else "/healthz" if ws.editor == "vscode" else "/"
    return ws.upstream_url.rstrip("/") + suffix


def http_ready(ws):
    try:
        response = httpx.get(ready_url(ws),timeout=1.5,follow_redirects=False,trust_env=False)
        if ws.editor == "sql":
            return response.status_code == 200 and response.json().get("health") == "ok"
        return 200 <= response.status_code < 400
    except (httpx.HTTPError,ValueError,AttributeError):
        return False


def refresh_workspace(db, orch, ws, settings, now=None):
    if ws.status not in ("starting","running"):
        return
    now = now or utcnow()
    started = ws.started_at or now
    started = started.replace(tzinfo=now.tzinfo) if started.tzinfo is None else started
    state = orch.status(ws.execution_ref) if ws.execution_ref else "pending"
    if state in ("missing","failed","succeeded"):
        starting = ws.status == "starting"
        ws.status = "failed"
        ws.startup_error = "The editor exited before it became ready. Retry or ask an administrator to check its startup logs." if starting else "The editor container stopped unexpectedly. Start the workspace again."
        # Preserve the container and its logs for diagnosis until retry/stop.
        return
    if ws.status == "running" and state == "running":
        return
    if ws.status == "running":
        ws.started_at = started = now
    ws.status = "starting"
    ws.waiting_reason = getattr(orch, "waiting_reason", lambda ref: None)(ws.execution_ref) if ws.execution_ref and state == "pending" else None
    if ws.upstream_url and state == "running" and (settings.orchestrator == "fake" or http_ready(ws)):
        ws.status, ws.startup_error, ws.waiting_reason = "running", None, None
    elif now - started >= timedelta(seconds=settings.workspace_start_timeout_seconds):
        ws.status = "failed"
        ws.startup_error = (f"The editor did not start within {settings.workspace_start_timeout_seconds} seconds: {ws.waiting_reason}. "
                            "Stop other workspaces or jobs, or ask an administrator for more capacity." if ws.waiting_reason else
                            f"The editor did not become ready within {settings.workspace_start_timeout_seconds} seconds. Stop it and retry, or ask an administrator to check its startup logs.")
        ws.waiting_reason = None
        if ws.execution_ref:
            orch.stop(ws.execution_ref)
        ws.execution_ref, ws.upstream_url = None, None


def refresh_all(session_factory, orch, settings):
    with session_factory() as db:
        for ws in db.scalars(select(Workspace).where(Workspace.status.in_(("starting","running"))).with_for_update(skip_locked=True)):
            refresh_workspace(db,orch,ws,settings)
        db.commit()


async def readiness_loop(session_factory, orch, settings):
    while True:
        await asyncio.sleep(2)
        try:
            await asyncio.to_thread(refresh_all,session_factory,orch,settings)
        except Exception:
            log.exception("workspace readiness check failed")
