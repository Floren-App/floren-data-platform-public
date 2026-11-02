"""Secret names, writes, binding and use are independently configurable capabilities."""
from fastapi import HTTPException
from . import permissions,rbac
from .models import SecretEntry, ServiceAccount


def target(scope,owner,name=''):
    return 'secret:'+scope+':'+owner+('/'+name if name else '')


def allowed(db,user,action,scope,owner,name=''):
    user=rbac.resolve_user(db,user)
    if scope=='user' and owner!=user.username:
        return False
    if scope in ('user','group') and user.environment_id:
        from .layers import canonical,is_production
        if is_production(db,canonical(db,user.environment_id)):
            return False
    sa=None
    if scope=='sa':
        sa=db.query(ServiceAccount).filter(ServiceAccount.name==owner).first()
        if not sa or sa.disabled:
            return False
    entry=db.query(SecretEntry).filter(SecretEntry.scope==scope,SecretEntry.owner==owner,SecretEntry.name==name).first() if name else None
    if entry and scope in ('user','sa'):
        expected=user.principal_id if scope=='user' else rbac.service_account_resource(db,sa).config['principal_id']
        if entry.owner_principal_id!=expected:
            return False
    if permissions.allowed(db,user,action,target(scope,owner,name)):
        return True
    if scope=='sa':
        return bool(sa and not sa.disabled and permissions.domain_allowed(db,user,action,
            rbac.service_account_resource(db,sa),'identity'))
    return False


def require(db,user,action,scope,owner,name=''):
    if not allowed(db,user,action,scope,owner,name):
        raise HTTPException(403,action+' permission required')
