"""Files the control plane reads for runs and configs.

Workspace runs: from the user's workspace volume, via the running workspace container.
Deployed runs: from their own folder of the shared data volume, which the control plane mounts.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from fastapi import HTTPException

from .config import get_settings
from .models import Run, Workspace, utcnow
from .orchestrator import Orchestrator
from .programs import CONFIG_FILE, WORKSPACE, Resolved, merge_configs
from .schemas import ConfigOut

CONFIG_LIMIT = 256 * 1024
CONSOLE_LIMIT = 2 * 1024 * 1024


def _running_ref(ws: Optional[Workspace]) -> Optional[str]:
    return ws.execution_ref if ws and ws.status == "running" and ws.execution_ref else None


def _cached_files(ws: Workspace) -> Dict[str, str]:
    if not ws.config_text:
        return {}
    try:
        data = json.loads(ws.config_text)
    except json.JSONDecodeError:
        return {"": ws.config_text}
    # Current format {"files": {folder: text}}; older rows hold one fdp.json as-is.
    return data["files"] if isinstance(data, dict) and set(data) == {"files"} else {"": ws.config_text}


def config_out(files: Dict[str, str], live: bool, read_at, *, db: Session, layer_id: str) -> Tuple[ConfigOut, Dict[str, Resolved]]:
    if not files:
        return ConfigOut(exists=False, live=live, read_at=read_at), {}
    programs, warehouse, errors = merge_configs(files)
    from .layers import canonical, program_layer
    actual = canonical(db, layer_id)
    visible = {}
    for name, resolved in programs.items():
        try:
            declared = program_layer(db, resolved.program)
        except HTTPException as exc:
            errors.append(f"{name}: {exc.detail}")
            continue
        if declared is None or declared.id == actual:
            visible[name] = resolved
    programs = visible
    config = {
        "warehouse": warehouse,
        "programs": {name: {**r.program.model_dump(mode="json", by_alias=True), "folder": r.folder} for name, r in programs.items()},
    }
    return ConfigOut(exists=True, live=live, read_at=read_at, errors=errors, config=config), programs


def load_config(db: Session, orch: Orchestrator, ws: Workspace) -> Tuple[ConfigOut, Dict[str, Resolved]]:
    """Every fdp.json in the workspace root and its direct subfolders (cloned repos).

    Read live when the workspace runs (and cached), otherwise from the cache.
    """
    live = False
    ref = _running_ref(ws)
    if ref:
        files: Dict[str, str] = {}
        for rel, _size in orch.list_files(ref, WORKSPACE, name=CONFIG_FILE, maxdepth=2):
            raw = orch.read_file(ref, f"{WORKSPACE}/{rel}", CONFIG_LIMIT)
            if raw is not None:
                files[os.path.dirname(rel)] = raw.decode("utf-8", errors="replace")
        ws.config_text = json.dumps({"files": files}) if files else None
        ws.config_read_at = utcnow()
        db.commit()
        live = True
    return config_out(_cached_files(ws), live, ws.config_read_at, db=db, layer_id=ws.environment_id)


# --- run consoles and outputs ---------------------------------------------------------------

def data_run_dir(run_id: str) -> str:
    """A deployed run's folder on the shared data volume, as the control plane sees it."""
    return os.path.join(get_settings().data_dir, "runs", run_id)


def run_dir(run: Run) -> str:
    return f"{WORKSPACE}/.fdp/runs/{run.id}"


def _local_read(path: str, limit: int) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return None


def _deployed(orch: Orchestrator, run: Run):
    """A deployed run's folder: the shared data volume, or its managed layer's artifact volume."""
    from sqlalchemy.orm import object_session
    from .artifacts import for_run
    return for_run(object_session(run), run, orch)


def console(orch: Orchestrator, ws: Optional[Workspace], run: Run, tail: int) -> str:
    """Live container logs while the run container exists; afterwards the console.log it left behind."""
    text = orch.logs(run.execution_ref, tail=tail) if run.execution_ref else ""
    if text:
        return text
    if run.workload_id:
        raw = _deployed(orch, run).read(f"runs/{run.id}/console.log", CONSOLE_LIMIT)
    else:
        ref = _running_ref(ws)
        raw = orch.read_file(ref, f"{run_dir(run)}/console.log", CONSOLE_LIMIT) if ref else None
    if raw is None:
        return ""
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(lines[-tail:])


def outputs(orch: Orchestrator, ws: Optional[Workspace], run: Run) -> Optional[List[Tuple[str, int]]]:
    """None when a workspace run's workspace isn't running (its outputs live on its volume)."""
    if run.workload_id:
        return _deployed(orch, run).list(f"runs/{run.id}/outputs")
    ref = _running_ref(ws)
    if not ref:
        return None
    return orch.list_files(ref, f"{run_dir(run)}/outputs")


def read_output(orch: Orchestrator, ws: Optional[Workspace], run: Run, path: str, limit: int) -> Optional[bytes]:
    if run.workload_id:
        return _deployed(orch, run).read(f"runs/{run.id}/outputs/{path}", limit)
    ref = _running_ref(ws)
    if not ref:
        return None
    return orch.read_file(ref, f"{run_dir(run)}/outputs/{path}", limit)
