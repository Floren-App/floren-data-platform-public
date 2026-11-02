"""Function permissions follow a catalog asset's physical target when one exists."""
from fastapi import HTTPException
from . import permissions, rbac
from .object_access import target_for_asset


def allowed(db,user,action,asset,resource,legacy_actions=()):
    target=target_for_asset(db,asset,resource) if resource.kind=='schema' else None
    target=target or resource
    from .layers import resource_layer
    layer=resource_layer(db,resource)
    return (permissions.allowed(db,user,action,'catalog',layer) or
        any(permissions.domain_allowed(db,user,action,target,legacy) for legacy in (legacy_actions or (None,))))


def require(db,user,action,asset,resource,legacy_actions=()):
    if not allowed(db,user,action,asset,resource,legacy_actions):
        raise HTTPException(403,action+' permission required')
