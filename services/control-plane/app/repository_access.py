"""Repository functionality is configurable independently of platform administration."""
from fastapi import HTTPException
from . import permissions,rbac


def target(repo):
    return 'repository:'+repo.id


def legacy(db,user,repo,manage=False):
    user=rbac.resolve_user(db,user)
    if user.is_admin:
        return True
    groups=rbac.effective_allowlist_groups(db,user)
    if repo.owner_group_id in groups:
        return True
    return not manage and (bool(repo.allowed_groups and groups & set(repo.allowed_groups)) or
        (not repo.allowed_groups and rbac.allowed(db,user,'deploy',rbac.root(db))))


def allowed(db,user,action,repo):
    return (bool(repo.id) and permissions.allowed(db,user,action,target(repo))) or legacy(db,user,repo,
        manage=action not in ('repositories.list','repositories.branches.read','repositories.manifest.read','repositories.pull'))


def require(db,user,action,repo):
    if not allowed(db,user,action,repo):
        raise HTTPException(403,action+' permission required')
