"""Run metadata ownership does not confer access to payloads or execution controls."""
from fastapi import HTTPException
from sqlalchemy import select
from . import permissions, rbac
from .models import Run, Workload

ACTIONS=('runs.list','runs.read','logs.read','artifacts.list','artifacts.read','artifacts.download','artifacts.open',
    'runs.cancel_own','runs.cancel_others','runs.retry')


def context_clause(db,user):
    """The runs an execution credential may reach, as a query condition; None when it is not bound to a context.

    A workspace credential reaches that workspace's runs, not those of the owner's other workspaces. A run credential
    reaches its own run and the runs of the workspace or workload it belongs to. A workload credential reaches that
    workload's runs and the deployed runs its identity started: an orchestrator's tasks. Interactive people and
    service tokens keep their identity-based access.
    """
    kind,_,ident=(user.execution_context or '').partition(':')
    started=Run.workload_id.is_not(None)&(Run.initiator_id==user.principal_id) if user.principal_id else Run.id.is_(None)
    if kind=='workspace':
        return Run.workspace_id==ident
    if kind=='workload':
        return (Run.workload_id==ident)|started
    if kind=='run':
        origin=db.get(Run,ident)
        if origin is None:
            return Run.id.is_(None)
        if origin.workload_id:
            return (Run.id==ident)|(Run.workload_id==origin.workload_id)|started
        return (Run.id==ident)|(Run.workspace_id==origin.workspace_id) if origin.workspace_id else Run.id==ident
    return None


def in_context(db,user,run):
    clause=context_clause(db,user)
    return clause is None or db.scalar(select(Run.id).where(Run.id==run.id,clause)) is not None


def allowed(db,user,action,run):
    if permissions.allowed(db,user,action,'run:'+run.id):
        return True
    # Explicit legacy workload execution remains compatible during migration.
    pub=db.get(Workload,run.workload_id) if run.workload_id else None
    if pub:
        return permissions.domain_allowed(db,user,action,rbac.workload_resource(db,pub),'read' if action in ('runs.list','runs.read') else 'execute' if action.startswith('runs.') else 'open')
    return False


def require(db,user,action,run):
    if not allowed(db,user,action,run):
        raise HTTPException(403,action+' permission required')


def capabilities(db,user,run):
    return [action for action in ACTIONS if allowed(db,user,action,run)]
