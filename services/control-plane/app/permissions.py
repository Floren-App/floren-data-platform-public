"""Named capabilities. Scope is resolved from server-owned resources, never a UI selector."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import or_, select

from . import rbac
from .app_profiles import acting_as_admin, interactive
from .authorization_cache import cached
from .models import CustomPermissionRole, PermissionGrant, Resource

REGISTRY = json.loads(Path(__file__).with_name("permission_registry.json").read_text())
ROLES = ("Viewer", "Executor", "Contributor", "Manager", "Admin", "Owner")


def validate_actions(actions):
    unknown = set(actions) - REGISTRY.keys()
    if unknown or not actions:
        raise HTTPException(422, "choose registered permissions" + (": " + ", ".join(sorted(unknown)) if unknown else ""))
    return sorted(set(actions))


def role_actions(namespace, role):
    if role not in ROLES:
        raise HTTPException(422, "unknown built-in permission role")
    result = [key for key, definition in REGISTRY.items()
              if key.startswith(namespace + ".") and role in definition["roles"]]
    if not result:
        raise HTTPException(422, "role has no permissions in this namespace")
    return sorted(result)


def resource_layer(db, target):
    """No implicit default layer for global collections; actual objects inherit explicit layer bindings."""
    resource = db.get(Resource, target)
    if resource is None:
        return None
    from .layers import explicit_layer
    if resource.kind == "layer":
        return resource.id
    for item in rbac.ancestors(db, resource):
        layer = explicit_layer(db, item)
        if layer:
            return layer
    return None


@cached
def grants(db, user):
    user = rbac.resolve_user(db, user)
    groups = rbac.app_groups(db, user)
    if interactive(user):
        groups = groups & {user.active_group_id}
    return list(db.scalars(select(PermissionGrant).where(or_(
        PermissionGrant.app_group_id.in_(list(groups)), PermissionGrant.principal_id == user.principal_id))))


def grant_actions(db, grant):
    if grant.custom_role_id:
        role = db.get(CustomPermissionRole, grant.custom_role_id)
        return set(role.actions) if role else set()
    if grant.role:
        return set(role_actions(grant.namespace, grant.role))
    return set(grant.actions or [])


def targets(db, target):
    resource = db.get(Resource, target)
    return {item.id for item in rbac.ancestors(db, resource)} if resource else {target}


def scope_matches(granted, actual):
    return granted == actual or (granted == "*" and actual is not None)


def allowed(db, user, action: str, target: str, layer_id: Optional[str] = None) -> bool:
    if action not in REGISTRY or not target:
        return False
    user = rbac.resolve_user(db, user)
    actual_layer = resource_layer(db, target)
    if actual_layer and layer_id and actual_layer != layer_id:
        return False
    layer = actual_layer or layer_id
    if user.environment_id and layer != user.environment_id:
        return False
    if acting_as_admin(user):
        return True
    chain = targets(db, target)
    held_roles = set()
    for grant in grants(db, user):
        if grant.target_id not in chain or not scope_matches(grant.layer_id, layer):
            continue
        if action in grant_actions(db, grant):
            return True
        if grant.role and action.startswith(grant.namespace + "."):
            held_roles.add(grant.role)
    required = REGISTRY[action].get("requires_roles", [])
    if required and set(required).issubset(held_roles):
        return True
    return False


def require(db, user, action, target, layer_id=None):
    if not allowed(db, user, action, target, layer_id):
        raise HTTPException(403, f"{action} permission required")
    return rbac.resolve_user(db, user)


def can_delegate(db, user, namespace, target, layer, actions):
    """A delegator must own this capability domain and hold every capability being delegated."""
    user = rbac.resolve_user(db, user)
    if acting_as_admin(user):
        return True
    if layer == "*":
        return False
    chain = targets(db, target)
    owns = any(g.role == "Owner" and g.namespace == namespace and g.target_id in chain
               and scope_matches(g.layer_id, layer) for g in grants(db, user))
    return owns and all(allowed(db, user, action, target, layer) for action in actions)


def capabilities(db, user, target, layer=None):
    return sorted(key for key in REGISTRY if allowed(db, user, key, target, layer))
