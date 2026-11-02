"""Warehouse grants supplement explicitly configured legacy shared/group access."""
from . import permissions,rbac


def target(pool):
    return 'warehouse:'+pool.id


def legacy(db,user,pool):
    user=rbac.resolve_user(db,user)
    return user.is_admin or not pool.allowed_groups or bool(set(pool.allowed_groups)&rbac.effective_allowlist_groups(db,user))


def allowed(db,user,action,pool,layer=None):
    return permissions.allowed(db,user,action,target(pool),layer) or legacy(db,user,pool)


def visible(db,user,pool,layer=None):
    if allowed(db,user,'compute.pools.list',pool,layer):
        return True
    from .layers import all_layers
    return layer is None and any(permissions.allowed(db,user,'compute.pools.list',target(pool),r.id) for r in all_layers(db))
