"""Compatibility names for server-derived layer isolation across all RBAC consumers.

Interactive humans may use several layers; every execution context has exactly one.
"""
from dataclasses import replace
from fastapi import HTTPException
from sqlalchemy import select
from .auth import User
from .models import Resource, Workspace, Workload, Run, DirectoryPrincipal


def resource_environment(db, resource):
    from .layers import resource_layer
    return resource_layer(db, resource)


def principal_environment(db, principal_id):
    p = db.get(DirectoryPrincipal, principal_id)
    if not p:
        from .layers import default_layer
        return default_layer(db) or ""
    external_id = p.external_id[4:] if p.external_id.startswith("api:") else p.external_id
    resource = db.scalar(select(Resource).where(Resource.kind.in_(("service", "api")), Resource.object_id == external_id))
    return resource_environment(db, resource)


def permits(db, user, resource):
    from .platform_metadata import is_resource, authorized
    if is_resource(resource):
        from .config import get_settings
        return get_settings().metadata_sql_enabled and authorized(user)
    from .layers import canonical
    return user.environment_id is None or resource_environment(db, resource) == canonical(db, user.environment_id)


def bind(db, user, resource):
    environment = resource_environment(db, resource)
    from .layers import canonical
    if user.environment_id is not None and canonical(db, user.environment_id) != environment:
        raise HTTPException(403, "cross-layer execution is forbidden")
    return replace(user, environment_id=environment)


def execution_user(db, context):
    primary_group = None
    if isinstance(context, dict):
        sub, pid = context["sub"], context.get("pid", "")
        ws_id, pub_id, run_id = context.get("ws"), context.get("pub"), context.get("run")
    else:
        sub, pid = context.username, context.principal_id
        ws_id, pub_id, run_id = context.workspace_id, context.workload_id, context.run_id
    if run_id:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(401, "run not found")
        primary_group = run.primary_group_id
        ws_id, pub_id = run.workspace_id, run.workload_id
    if ws_id:
        ws = db.get(Workspace, ws_id)
        if not ws:
            raise HTTPException(401, "workspace not found")
        from .layers import canonical
        env, binding = canonical(db, ws.environment_id), f"workspace:{ws_id}"
    elif pub_id:
        from .rbac import workload_resource
        pub = db.get(Workload, pub_id)
        if not pub:
            raise HTTPException(401, "workload not found")
        env, binding = resource_environment(db, workload_resource(db, pub)), f"workload:{pub_id}"
    else:
        env, binding = principal_environment(db, pid), "service-token"
    return User(sub, principal_id=pid, environment_id=env, execution_context=f"run:{run_id}" if run_id else binding,
                active_group_id=primary_group)


def run_permitted(db, user, run):
    if user.environment_id is None:
        return True
    ctx = execution_user(db, {"sub": run.owner, "pid": run.principal_id, "run": run.id})
    return user.environment_id == ctx.environment_id


def validate_environment(db, environment_id):
    from .layers import require_layer
    return require_layer(db, environment_id) if environment_id else None


def workspace_environment(db, user, environment_id):
    from .layers import require_layer
    layer = require_layer(db, environment_id)
    if not layer.config.get("workspaces", False):
        raise HTTPException(403, "Workspaces are disabled in this layer. An administrator can enable them under Administration → Layers.")
    # Workspace creation is not a grant on a project folder. Data/execution access
    # still requires the person's RBAC rights AND this selected layer on every call.
    return layer.id


def check_branch(db, parent, branch):
    from .rbac import ancestors
    for folder in ancestors(db, parent):
        if folder.config.get("environment") and folder.config.get("branch") and branch != folder.config["branch"]:
            raise HTTPException(422, f"this environment requires branch {folder.config['branch']}")
