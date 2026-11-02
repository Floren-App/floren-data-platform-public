"""Private workspace functionality, scoped to its server-owned layer and stable ID."""
from fastapi import HTTPException
from . import permissions

ROOT='workspaces'
PREFIX='workspace:'
ACTIONS=('runs.create','workspaces.object.read','workspaces.object.start','workspaces.object.stop',
    'workspaces.object.editor.open','workspaces.object.settings.edit','workspaces.object.delete',
    'workspaces.files.list','workspaces.files.read','workspaces.files.download',
    'workspaces.files.write','workspaces.files.delete','workspaces.files.checkout','workspaces.files.pull')


def target(workspace):
    return PREFIX+workspace.id


def allowed(db,user,action,workspace):
    from .layers import canonical
    return permissions.allowed(db,user,action,target(workspace),canonical(db,workspace.environment_id))


def require(db,user,action,workspace):
    if not allowed(db,user,action,workspace):
        raise HTTPException(403,action+' permission required')


def output(db,user,workspace):
    workspace.capabilities=[action for action in ACTIONS if allowed(db,user,action,workspace)]
    if permissions.allowed(db,user,"git.credentials.binding.change","git-credentials"):
        workspace.capabilities.append("git.credentials.binding.change")
    return workspace
