"""Who is acting and whether a workspace is ready for its workflow, explained with the existing evaluator.

Readiness explains; it never authorizes. Each action still checks permissions, policy, capacity and state when it
happens. A check that fails to run reports "unknown" rather than "ready".
"""
from fastapi import HTTPException

from . import permissions, rbac, workspace_access
from .models import AppGroup, Resource, utcnow
from .schemas import ActingContext, ReadinessCheck, WorkspaceReadiness

KINDS = {"workspace": "editor", "run": "job", "workload": "workload"}


def acting_context(db, user, layer_id=None) -> ActingContext:
    user = rbac.resolve_user(db, user)
    kind = KINDS.get(user.execution_context.partition(":")[0]) if user.execution_context else None
    if kind is None:
        kind = "service" if user.username.startswith(("service:", "sa:")) else "person"
    group = db.get(AppGroup, user.active_group_id) if user.active_group_id else None
    from .layers import canonical
    layer_id = canonical(db, layer_id or user.environment_id) if (layer_id or user.environment_id) else None
    layer = db.get(Resource, layer_id) if layer_id else None
    return ActingContext(kind=kind, username=user.username, group_id=group.id if group else None,
                         group_name=group.name if group else None, layer_id=layer_id, layer_name=layer.name if layer else None)


def _permission(db, user, ws, key, label, action, *, collection=None, needed_for=""):
    from .layers import canonical
    layer = canonical(db, ws.environment_id)
    targets = [workspace_access.target(ws)] + ([collection] if collection else [])
    if any(permissions.allowed(db, user, action, t, layer) for t in targets):
        return ReadinessCheck(key=key, label=label, state="ready", permission=action)
    return ReadinessCheck(key=key, label=label, state="missing", permission=action,
                          detail=f"Your group does not have {action}{needed_for}.")


def _config(db, orch, user, ws):
    check = _permission(db, user, ws, "config", "Read fdp.json", "workspaces.files.read", needed_for=", so programs can't be listed")
    if check.state != "ready":
        return check
    if ws.status != "running":
        return check.model_copy(update={"state": "unknown", "detail": "Start the workspace to read its fdp.json."})
    from .workspace_files import load_config
    try:
        out, _ = load_config(db, orch, ws)
    except Exception:
        return check.model_copy(update={"state": "unknown", "detail": "The workspace's files could not be read just now."})
    if not out.exists:
        return check.model_copy(update={"state": "unavailable", "detail": "The workspace has no fdp.json; ad-hoc files can still run."})
    if out.errors:
        return check.model_copy(update={"state": "unavailable", "detail": "fdp.json is invalid: " + "; ".join(out.errors[:3])})
    return check


def _warehouse(db, user, ws):
    from . import capacity
    from .layers import canonical
    from .routes.common import require_warehouse
    layer = canonical(db, ws.environment_id)
    name = capacity.choose_warehouse(db, None, None, layer, "script")
    if not name:
        return ReadinessCheck(key="warehouse", label="Warehouse", state="missing",
                              detail="No default warehouse is set for this layer; choose one per run, or ask an administrator to set one.")
    try:
        require_warehouse(db, name, user, layer)
    except HTTPException:
        return ReadinessCheck(key="warehouse", label="Warehouse", state="missing", permission="compute.pools.use",
                              detail=f"The default warehouse {name} is not available to your group.")
    return ReadinessCheck(key="warehouse", label="Warehouse", state="ready", detail=f"Runs use {name} unless a program says otherwise.")


def workspace_readiness(db, orch, user, ws) -> WorkspaceReadiness:
    user = rbac.resolve_user(db, user)
    running = ws.status == "running"
    checks = [
        ReadinessCheck(key="workspace", label="Workspace running", state="ready" if running else "unavailable",
                       detail="" if running else f"The workspace is {ws.status}; start it to run programs."),
        _config(db, orch, user, ws),
        _permission(db, user, ws, "runs", "Start runs", "runs.create", needed_for=", so it can't start runs"),
        _warehouse(db, user, ws),
        # Logs and outputs are separate from running: an Executor may run work without reading what it produced.
        _permission(db, user, ws, "logs", "Read run logs", "logs.read", collection="runs",
                    needed_for="; runs still start, but their logs are hidden"),
        _permission(db, user, ws, "outputs", "Read run outputs", "artifacts.read", collection="runs",
                    needed_for="; runs still start, but their outputs are hidden"),
    ]
    group = db.get(AppGroup, ws.owner_group_id) if ws.owner_group_id else None
    return WorkspaceReadiness(context=acting_context(db, user, ws.environment_id), workspace_group_id=ws.owner_group_id,
                              workspace_group_name=group.name if group else None, checks=checks, checked_at=utcnow())
