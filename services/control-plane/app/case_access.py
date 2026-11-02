"""Case function grants supplement requester, ownership and assigned-review-duty boundaries."""
from . import permissions


def allowed(db,user,action,case):
    return permissions.allowed(db,user,action,'case:'+case.id,case.layer_id)


def require(db,user,action,case):
    permissions.require(db,user,action,'case:'+case.id,case.layer_id)


def is_requester(db,user,case):
    from . import rbac
    user=rbac.resolve_user(db,user)
    return bool(case.requester_principal_id and case.requester_principal_id==user.principal_id)
