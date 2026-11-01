from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import User, get_current_user, mint_token
from ..config import get_settings
from ..db import get_db
from ..deps import get_actor, get_orchestrator, granted_sources
from ..models import ComputePool, Resource, Workspace, utcnow
from ..orchestrator import Orchestrator
from ..schemas import ActiveGroupIn, ActiveLayerIn, ConfigOut, Me, OpenOut, PoolOut, WorkspaceIn, WorkspaceOut
from ..executions import workspace_spec
from ..workspace_files import load_config
from .. import git_credentials
from .common import audit, ds_out, pools_for

router = APIRouter(prefix="/v1", tags=["workspaces"])


@router.get("/me", response_model=Me)
def me(db: Session = Depends(get_db), user: User = Depends(get_current_user), orch: Orchestrator = Depends(get_orchestrator)):
    from ..rbac import resolve_user, DATA_ROLES
    from ..ownership import available_groups, GLOBAL_ADMIN, browsing_scope
    # The authentication dependency already reconciled OIDC/SCIM claims.
    user = resolve_user(db, user, refresh=not bool(user.principal_id))
    db.commit()
    from ..compute_usage import inventory, pool_usage
    items = inventory(orch)
    pools = pools_for(db, user.groups)
    pools = [p.model_copy(update=pool_usage(items, p.name)) for p in pools]
    browsing = browsing_scope(db,user)
    visible_sources = {r.object_id for r in db.scalars(select(Resource))
                       if r.kind in ("source","schema") and browsing.get(r.id,set()) & DATA_ROLES.keys()}
    from ..runtimes import r_enabled
    from ..app_profiles import acting_as_admin, features_for, profile_for, FEATURES
    from ..settings_access import granted as settings_granted
    profile = profile_for(db, user)
    app_features = [f for f in FEATURES if f in features_for(db, user)]
    db.commit()
    return Me(
        features={"r": r_enabled()},
        app_profile="Full access (administrator)" if acting_as_admin(user) else profile.name if profile else None,
        app_features=app_features,
        compliance_roles=compliance_roles(db, user),
        settings=settings_granted(db, user),
        principal_id=user.principal_id, active_group_id=user.active_group_id or None,
        active_layer_id=active_layer(db, user),
        available_groups=[{"id":g.id,"name":g.name,"system":g.id == GLOBAL_ADMIN}
                          for g in available_groups(db,user)],
        username=user.username,
        groups=user.groups,
        is_admin=user.is_admin,
        data_sources=[ds_out(ds) for ds in granted_sources(db, user) if ds.id in visible_sources],
        pools=pools,
    )


def compliance_roles(db, user):
    from ..compliance import ROLES
    from ..models import ComplianceRole
    from ..rbac import app_groups
    if user.is_admin:
        return list(ROLES)
    groups = app_groups(db, user)
    held = set(db.scalars(select(ComplianceRole.role).where(ComplianceRole.app_group_id.in_(groups)))) if groups else set()
    return [r for r in ROLES if r in held]


def selectable_layers(db, user):
    """The layers this caller can see (an execution sees only its own), as in GET /v1/layers."""
    from .. import layers
    return [layer.id for layer in layers.all_layers(db)
            if user.environment_id is None or layers.canonical(db, user.environment_id) == layer.id]


def active_layer(db, user):
    """The app's layer mode: the caller's choice while that layer exists, else the default layer."""
    from .. import layers
    from ..models import UserPreference
    choices = selectable_layers(db, user)
    preference = db.get(UserPreference, user.principal_id) if user.principal_id else None
    if preference is not None and preference.active_layer_id in choices:
        return preference.active_layer_id
    default = layers.default_layer(db)
    return default if default in choices else (choices[0] if len(choices) == 1 else None)


@router.put("/me/active-layer")
def select_active_layer(body: ActiveLayerIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from .. import rbac
    from ..models import UserPreference
    user = rbac.resolve_user(db, user)
    if not user.principal_id:
        raise HTTPException(401, "A provisioned directory identity is required")
    if body.layer_id not in selectable_layers(db, user):
        raise HTTPException(404, "Layer not found")
    preference = db.get(UserPreference, user.principal_id)
    if preference is None:
        preference = UserPreference(principal_id=user.principal_id)
        db.add(preference)
    before = preference.active_layer_id
    preference.active_layer_id = body.layer_id
    audit(db, user.username, "identity.active_layer", body.layer_id, before=before)
    db.commit()
    return {"active_layer_id": body.layer_id}


@router.put("/me/active-group")
def select_active_group(body: ActiveGroupIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from .. import rbac
    from ..ownership import available_groups
    from ..models import UserPreference
    user = rbac.resolve_user(db, user)
    group = next((g for g in available_groups(db,user) if g.id == body.group_id),None)
    if group is None:
        raise HTTPException(403, "Choose an app group available to your identity.")
    preference = db.get(UserPreference,user.principal_id)
    if preference is None:
        preference = UserPreference(principal_id=user.principal_id)
        db.add(preference)
    before = preference.active_group_id
    preference.active_group_id = group.id
    audit(db,user.username,"identity.active_group",group.id,before=before)
    db.commit()
    return {"active_group_id":group.id,"name":group.name}


def owned(db: Session, ws_id: str, user: User) -> Workspace:
    from ..rbac import resolve_user, same_identity
    user = resolve_user(db, user)
    ws = db.get(Workspace, ws_id)
    if not ws or (not same_identity(ws, user) and not user.is_admin) or (user.execution_context and user.execution_context != f"workspace:{ws.id}"):
        raise HTTPException(404, "workspace not found")
    return ws


@router.get("/workspaces", response_model=List[WorkspaceOut])
def list_workspaces(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..rbac import resolve_user
    user = resolve_user(db, user)
    query = select(Workspace)
    if not user.is_admin:
        query = query.where((Workspace.principal_id == user.principal_id) |
            (Workspace.principal_id.is_(None) & (Workspace.owner == user.username)))
    return db.scalars(query.order_by(Workspace.created_at)).all()


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201)
def create_workspace(body: WorkspaceIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..rbac import resolve_user
    user = resolve_user(db, user, refresh=True)
    if body.editor == "rstudio":
        from ..runtimes import require_kind
        require_kind("r")
    from ..environments import workspace_environment
    environment = workspace_environment(db, user, body.layer_id or body.environment_id)
    from ..ownership import owner_group
    group = owner_group(db,user)
    ws = Workspace(owner=user.username, principal_id=user.principal_id, environment_id=environment,
                   owner_group_id=group.id, name=body.name, editor=body.editor)
    db.add(ws)
    db.flush()
    # No shared home between workspaces, including those owned by the same human.
    ws.volume_owner = f"ws-{ws.id}"
    db.add(ws)
    db.commit()
    return ws


@router.post("/workspaces/{ws_id}/start", response_model=WorkspaceOut)
def start_workspace(
    ws_id: str,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
    user: User = Depends(get_current_user),
):
    ws = owned(db, ws_id, user)
    if ws.editor == "rstudio":
        from ..runtimes import require_kind
        require_kind("r")
    ws = db.scalar(select(Workspace).where(Workspace.id == ws.id).with_for_update().execution_options(populate_existing=True))
    if ws.status == "running" and ws.execution_ref and orch.status(ws.execution_ref) != "running":
        ws.status = "stopped"   # its container died (e.g. a Docker restart): start it again
    if ws.status not in ("running", "starting", "queued"):
        from ..layer_infrastructure import require_ready
        require_ready(db, ws.environment_id, 'workspace')
        from ..models import DirectoryPrincipal
        if ws.principal_id:
            db.scalar(select(DirectoryPrincipal).where(DirectoryPrincipal.id == ws.principal_id).with_for_update())
        from .. import capacity
        limits = capacity.workspace_settings(db)
        limit = limits["max_running_per_user"]
        running = db.scalars(select(Workspace).where(
            ((Workspace.principal_id == ws.principal_id) if ws.principal_id else (Workspace.owner == user.username)), Workspace.status.in_(("queued","starting","running")), Workspace.id != ws.id)).all()
        if len(running) >= limit:
            names = ", ".join(w.name for w in running)
            raise HTTPException(409, f"You can have {limit} workspaces running at a time ({names} "
                                     f"{'is' if len(running) == 1 else 'are'} running). Stop one first.")
        from ..environments import workspace_environment
        workspace_environment(db, user, ws.environment_id)
        total = limits["max_running"]
        if total is not None and (capacity.workspaces_running(db) >= total or capacity.workspace_queue(db)):
            # Everyone's workspaces share a total; this one starts when one stops, in arrival order.
            ws.status, ws.queued_at, ws.startup_error = "queued", utcnow(), None
            audit(db, user.username, "workspace.queue", ws.id, editor=ws.editor)
            db.commit()
            return ws
        launch_workspace(db, orch, ws)
    return ws


def template_repositories(db: Session, ws: Workspace) -> list:
    """(folder, branch, url) of the template's repositories, cloned at first start with the person's own token."""
    from ..models import GitRepository, WorkspaceTemplate
    template = db.get(WorkspaceTemplate, ws.template_id) if ws.template_id else None
    repos = [db.get(GitRepository, rid) for rid in (template.repositories or [])] if template else []
    return [(r.name, r.default_branch, r.url) for r in repos if r]


def launch_workspace(db: Session, orch: Orchestrator, ws: Workspace) -> Workspace:
    """Start the editor container (also called by the dispatcher for queued workspaces)."""
    from ..environments import execution_user
    from ..rbac import resolve_user
    from .. import capacity
    user = resolve_user(db, execution_user(db, {"sub": ws.owner, "pid": ws.principal_id, "ws": ws.id}))
    warehouse = capacity.workspace_settings(db)["warehouse"]
    pool = db.scalar(select(ComputePool).where(ComputePool.name == warehouse)) if warehouse else None
    spec = workspace_spec(get_settings(), ws, user.groups, granted_sources(db, user),
                          size=(pool.cpu, pool.memory_gb) if pool else None, clone=template_repositories(db, ws),
                          git_identity=git_credentials.identity(db, ws))
    from ..layer_infrastructure import placement
    spec.placement = placement(db, ws.environment_id)
    ws.status, ws.started_at, ws.startup_error, ws.queued_at = "starting", utcnow(), None, None
    try:
        handle = orch.start(spec)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("workspace %s container could not start",ws.id)
        ws.status = "failed"
        ws.startup_error = "The editor container could not be started. Check that its runtime image is installed and the compute service is available."
        db.commit()
        return ws
    ws.execution_ref, ws.upstream_url = handle.ref, handle.upstream_url
    if get_settings().orchestrator == "fake":
        from ..workspace_lifecycle import refresh_workspace
        refresh_workspace(db,orch,ws,get_settings())
    ws.last_activity_at = utcnow()
    audit(db, ws.owner, "workspace.start", ws.id, editor=ws.editor)
    db.commit()
    return ws


@router.post("/workspaces/{ws_id}/stop", response_model=WorkspaceOut)
def stop_workspace(
    ws_id: str,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
    user: User = Depends(get_current_user),
):
    ws = owned(db, ws_id, user)
    ws = db.scalar(select(Workspace).where(Workspace.id == ws.id).with_for_update().execution_options(populate_existing=True))
    if ws.execution_ref:
        orch.stop(ws.execution_ref)
    ws.status, ws.execution_ref, ws.upstream_url = "stopped", None, None
    ws.startup_error, ws.queued_at = None, None
    db.commit()
    return ws


@router.delete("/workspaces/{ws_id}", status_code=204)
def delete_workspace(
    ws_id: str,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
    user: User = Depends(get_current_user),
):
    ws = owned(db, ws_id, user)
    ws = db.scalar(select(Workspace).where(Workspace.id == ws.id).with_for_update().execution_options(populate_existing=True))
    if ws.execution_ref:
        orch.stop(ws.execution_ref)
    if ws.template_id and ws.principal_id:  # deleted on purpose: the group template does not recreate it
        from ..models import WorkspaceTemplateDismissal
        db.add(WorkspaceTemplateDismissal(template_id=ws.template_id, principal_id=ws.principal_id))
    db.delete(ws)
    db.commit()


@router.get("/pools", response_model=List[PoolOut])
def list_pools(layer_id: Optional[str] = None, kind: Optional[Literal["script", "notebook", "app", "dbt", "airflow"]] = None,
               db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Warehouses this user may run on. Works with execution tokens (editor runner panels). With layer_id and/or
    kind, only warehouses meant for that work; an editor's own layer applies when no layer is given."""
    if layer_id:
        from ..layers import canonical
        layer_id = canonical(db, layer_id)
    elif kind and user.environment_id:
        layer_id = user.environment_id
    return pools_for(db, user.groups, layer_id, kind)


@router.get("/workspaces/{ws_id}/config", response_model=ConfigOut)
def workspace_config(
    ws_id: str,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
    user: User = Depends(get_actor),
):
    """The workspace's fdp.json: programs, their arguments and default warehouses."""
    out, _ = load_config(db, orch, owned(db, ws_id, user))
    return out


@router.post("/workspaces/{ws_id}/open", response_model=OpenOut)
def open_workspace(ws_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Returns a one-time URL; the gateway trades the ticket for a path-scoped cookie."""
    ws = owned(db, ws_id, user)
    if ws.status not in ("starting","running"):
        raise HTTPException(409, ws.startup_error or "Start the workspace before opening its editor.")
    ticket = mint_token("ticket", user.username, 60, target=f"ws:{ws.id}", pid=user.principal_id)
    return OpenOut(url=f"{get_settings().public_url}/ws/{ws.id}/?dc_ticket={ticket}")


@router.post("/executions/token")
def refresh_run_token(authorization: str = Header(default="")):
    """Active jobs may renew their own context, including after expiry; never renew a stopped run."""
    import jwt
    from ..auth import validate_execution
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "execution credential required")
    try:
        from ..auth import decode_session
        p = decode_session(authorization[7:], verify_exp=False)
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid execution credential") from None
    if p.get("typ") != "execution" or not p.get("run"):
        raise HTTPException(401, "only active job tokens can be renewed")
    validate_execution(p)
    return {"token": mint_token("execution", p["sub"], get_settings().session_token_ttl_seconds,
                               run=p["run"], ws=p.get("ws", ""), pub=p.get("pub", ""),
                               pid=p.get("pid", ""), sha=p.get("sha", ""))}
