"""The central workload log. Programs write with fdp.log; people read what they may see.

Writing needs a run's or workload's execution token: the platform stamps the run, workload, workspace,
commit and layer from it, so a program cannot log on behalf of another. Reading follows run and workload
visibility: entries of a run are visible to whoever may see that run, entries of a workload (deploys,
Airflow DAG runs, app lines) to whoever may read the workload."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import rbac
from ..auth import Execution, User, get_execution
from ..db import get_db
from ..deps import get_actor
from ..models import Run, Workload, WorkloadLog, Workspace, Resource, utcnow
from .. import workload_logs

router = APIRouter(prefix="/v1/logs", tags=["logs"])

Level = Literal["debug", "info", "warning", "error"]
RUN_CAP = 10_000            # entries one run may write
WORKLOAD_HOURLY_CAP = 5_000  # entries per hour from a workload without a run (apps, Airflow)


class LogIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Level = "info"
    message: str = Field(max_length=workload_logs.MAX_MESSAGE)
    fields: Dict[str, Any] = Field(default_factory=dict)
    event: str = Field(default="", max_length=48, pattern=r"^[a-z0-9_.-]*$",
                       description="Optional event name, e.g. dag.started (Airflow listener)")
    at: Optional[datetime] = Field(default=None, description="When it happened (kept when within a day of now)")


class LogBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: List[LogIn] = Field(min_length=1, max_length=100)


class LogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    at: datetime
    level: str
    source: str
    event: str
    message: str
    fields: Dict[str, Any]
    workload_id: Optional[str] = None
    workload: Optional[str] = Field(default=None, description="The workload's title")
    run_id: Optional[str] = None
    workspace_id: Optional[str] = None
    commit_sha: Optional[str] = None
    layer_id: Optional[str] = None
    actor: str = ""


class LogPage(BaseModel):
    entries: List[LogOut]
    next_before: Optional[int] = Field(default=None, description="Pass as before= for older entries")


def _when(value: Optional[datetime]) -> datetime:
    now = utcnow()
    if value is None:
        return now
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value if abs(now - value) <= timedelta(days=1) else now


@router.post("", status_code=202)
def write_logs(body: LogBatchIn, db: Session = Depends(get_db), wl: Execution = Depends(get_execution)):
    """Log lines from a running program, app or Airflow deployment (fdp.log)."""
    run = db.get(Run, wl.run_id) if wl.run_id else None
    workload = db.get(Workload, wl.workload_id) if wl.workload_id else (db.get(Workload, run.workload_id) if run and run.workload_id else None)
    if run is None and workload is None and not wl.workspace_id:
        raise HTTPException(403, "logs are written by runs, workloads and workspaces")
    if run is None and workload is not None and (workload.status == "stopped" or wl.username != workload.identity):
        raise HTTPException(403, "only the workload's own execution may write its log")
    if run is not None:
        written = db.scalar(select(func.count()).select_from(WorkloadLog).where(
            WorkloadLog.run_id == run.id, WorkloadLog.source != "platform")) or 0
        room = RUN_CAP - written
    elif workload is not None:
        written = db.scalar(select(func.count()).select_from(WorkloadLog).where(
            WorkloadLog.workload_id == workload.id, WorkloadLog.run_id.is_(None), WorkloadLog.source != "platform",
            WorkloadLog.at >= utcnow() - timedelta(hours=1))) or 0
        room = WORKLOAD_HOURLY_CAP - written
    else:
        room = RUN_CAP
    accepted = body.entries[:max(room, 0)]
    source = "airflow" if workload is not None and workload.kind == "airflow" and run is None else "program"
    for line in accepted:
        workload_logs.entry(db, level=line.level, message=line.message, fields=line.fields, event_name=line.event,
                            source=source, run=run, workload=workload if run is None else None,
                            workspace_id=wl.workspace_id or None, commit_sha=wl.commit_sha or None,
                            actor=wl.username, at=_when(line.at))
    if len(accepted) < len(body.entries) and room > 0:  # once, when the limit is reached
        workload_logs.entry(db, level="warning", message="log limit reached: further lines are dropped",
                            event_name="log.limit", run=run, workload=workload if run is None else None,
                            workspace_id=wl.workspace_id or None, actor="platform")
    db.commit()
    return {"accepted": len(accepted), "dropped": len(body.entries) - len(accepted)}


class _Visibility:
    """Caches per-request decisions: which runs and workloads this person may see."""

    def __init__(self, db: Session, user: User):
        self.db, self.user, self.runs, self.workloads = db, user, {}, {}

    def workload(self, workload_id: str) -> bool:
        if workload_id not in self.workloads:
            pub = self.db.get(Workload, workload_id)
            self.workloads[workload_id] = bool(pub) and (self.user.is_admin or rbac.allowed(
                self.db, self.user, "read", rbac.workload_resource(self.db, pub)))
        return self.workloads[workload_id]

    def run(self, run_id: str) -> bool:
        if run_id not in self.runs:
            from .runs import owned_run
            try:
                owned_run(self.db, run_id, self.user)
                self.runs[run_id] = True
            except HTTPException:
                self.runs[run_id] = False
        return self.runs[run_id]

    def entry(self, row: WorkloadLog) -> bool:
        if self.user.is_admin:
            return True
        if row.run_id:
            return self.run(row.run_id)
        if row.workload_id:
            return self.workload(row.workload_id)
        if row.workspace_id:
            ws = self.db.get(Workspace, row.workspace_id)
            return bool(ws) and rbac.same_identity(ws, self.user)
        return False


@router.get("", response_model=LogPage)
def read_logs(workload_id: str = "", folder_id: str = "", run_id: str = "", layer_id: str = "",
              level: Level = "debug", q: str = Query(default="", max_length=200),
              before: Optional[int] = Query(default=None, ge=1), limit: int = Query(default=100, ge=1, le=500),
              db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Newest first. level is the minimum shown (warning: warnings and errors). folder_id: workloads in that
    Workloads folder and its subfolders ("workloads" for all)."""
    user = rbac.resolve_user(db, user)
    stmt = select(WorkloadLog)
    if run_id:
        stmt = stmt.where(WorkloadLog.run_id == run_id)
    if workload_id:
        stmt = stmt.where(WorkloadLog.workload_id == workload_id)
    if folder_id and folder_id != "workloads":
        folders, frontier = {folder_id}, {folder_id}
        while frontier:
            frontier = set(db.scalars(select(Resource.id).where(Resource.parent_id.in_(frontier), Resource.kind == "folder"))) - folders
            folders |= frontier
        inside = select(Workload.id).join(Resource, Resource.id == Workload.resource_id).where(Resource.parent_id.in_(folders))
        stmt = stmt.where(WorkloadLog.workload_id.in_(inside))
    elif folder_id == "workloads":
        stmt = stmt.where(WorkloadLog.workload_id.is_not(None))
    if layer_id:
        stmt = stmt.where((WorkloadLog.layer_id == layer_id) | WorkloadLog.layer_id.is_(None))
    if level != "debug":
        stmt = stmt.where(WorkloadLog.level.in_(workload_logs.LEVELS[workload_logs.LEVELS.index(level):]))
    if q:
        stmt = stmt.where(WorkloadLog.message.ilike("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"))
    if before:
        stmt = stmt.where(WorkloadLog.id < before)
    visible = _Visibility(db, user)
    titles: Dict[str, str] = {}
    out: List[LogOut] = []
    cursor = None
    # Scan newest first in pages until enough visible entries are found (bounded).
    for _ in range(10):
        page = (stmt.where(WorkloadLog.id < cursor) if cursor else stmt).order_by(WorkloadLog.id.desc()).limit(limit * 2)
        rows = list(db.scalars(page))
        if not rows:
            cursor = None
            break
        for row in rows:
            cursor = row.id
            if not visible.entry(row):
                continue
            if row.workload_id and row.workload_id not in titles:
                pub = db.get(Workload, row.workload_id)
                titles[row.workload_id] = pub.title if pub else ""
            item = LogOut.model_validate(row)
            item.workload = titles.get(row.workload_id or "") or None
            out.append(item)
            if len(out) == limit:
                return LogPage(entries=out, next_before=row.id)
        if len(rows) < limit * 2:
            cursor = None
            break
    return LogPage(entries=out, next_before=cursor)
