"""Run metadata ownership does not confer access to payloads or execution controls."""
from fastapi import HTTPException
from . import permissions, rbac
from .models import Workload

ACTIONS=('runs.list','runs.read','logs.read','artifacts.list','artifacts.read','artifacts.download','artifacts.open',
    'runs.cancel_own','runs.cancel_others','runs.retry')


def allowed(db,user,action,run):
    if permissions.allowed(db,user,action,'run:'+run.id):
        return True
    # Explicit legacy workload execution remains compatible during migration.
    pub=db.get(Workload,run.workload_id) if run.workload_id else None
    if pub:
        return permissions.domain_allowed(db,user,action,rbac.workload_resource(db,pub),'read' if action in ('runs.list','runs.read') else 'execute')
    return False


def require(db,user,action,run):
    if not allowed(db,user,action,run):
        raise HTTPException(403,action+' permission required')


def capabilities(db,user,run):
    return [action for action in ACTIONS if allowed(db,user,action,run)]
