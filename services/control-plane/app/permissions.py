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


@cached
def resource_layer(db, target):
    """No implicit default layer for global collections; actual objects inherit explicit layer bindings."""
    if target.startswith('case:'):
        from .models import AccessCase
        case=db.get(AccessCase,target[len('case:'):])
        return case.layer_id if case else None
    if target.startswith('secret:sa:'):
        from .models import ServiceAccount
        owner=target[len('secret:sa:'):].split('/',1)[0]
        sa=db.query(ServiceAccount).filter(ServiceAccount.name==owner).first()
        return resource_layer(db,rbac.service_account_resource(db,sa).id) if sa else None
    if target.startswith('run:'):
        from .models import Run, Workload
        from .layers import canonical
        run=db.get(Run,target[len('run:'):])
        if not run:
            return None
        if run.environment_id:
            return canonical(db,run.environment_id)
        if run.workspace_id:
            return resource_layer(db,'workspace:'+run.workspace_id)
        if run.workload_id:
            pub=db.get(Workload,run.workload_id)
            return resource_layer(db,rbac.workload_resource(db,pub).id) if pub else None
        return canonical(db,run.layer_id)
    if target.startswith('workspace:'):
        from .models import Workspace
        from .layers import canonical
        workspace=db.get(Workspace,target[len('workspace:'):])
        return canonical(db,workspace.environment_id) if workspace else None
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
    groups = rbac.effective_app_groups(db, user)
    return list(db.scalars(select(PermissionGrant).where(or_(
        PermissionGrant.app_group_id.in_(list(groups)), PermissionGrant.principal_id == user.principal_id))))


@cached
def grant_actions(db, grant):
    if grant.custom_role_id:
        role = db.get(CustomPermissionRole, grant.custom_role_id)
        return set(role.actions) if role else set()
    if grant.role:
        return set(role_actions(grant.namespace, grant.role))
    return set(grant.actions or [])


@cached
def targets(db, target):
    if target.startswith('principal:'):
        return {target,'people'}
    if target.startswith('custom-role:'):
        return {target,'roles'}
    if target.startswith('warehouse:'):
        return {target,'set-pools'}
    if target.startswith('group:'):
        return {target,'set-groups'}
    if target.startswith('git-credential:'):
        return {target,'git-credentials'}
    if target.startswith('case:'):
        from .models import AccessCase
        from .compliance import case_target
        case=db.get(AccessCase,target[len('case:'):])
        chain={target,'cases'}
        parent=case_target(db,case) if case else None
        if parent:
            chain.update(targets(db,parent))
        return chain
    if target.startswith('secret:'):
        prefix=target.split('/',1)[0]
        chain={target,prefix,'secrets'}
        if prefix.startswith('secret:user:'):
            chain.add('personal-secrets')
        if prefix.startswith('secret:sa:'):
            from .models import ServiceAccount
            sa=db.query(ServiceAccount).filter(ServiceAccount.name==prefix[len('secret:sa:'):]).first()
            if sa:
                chain.update(targets(db,rbac.service_account_resource(db,sa).id))
        return chain
    if target.startswith('run:'):
        from .models import Run, Workload
        run=db.get(Run,target[len('run:'):])
        chain={target,'runs'}
        if run:
            if run.workspace_id:
                chain.update(targets(db,'workspace:'+run.workspace_id))
            if run.workload_id:
                pub=db.get(Workload,run.workload_id)
                if pub:
                    chain.update(targets(db,rbac.workload_resource(db,pub).id))
        return chain
    if target.startswith('workspace:'):
        layer=resource_layer(db,target)
        return {target,'workspaces',layer} if layer else {target}
    if target.startswith('repository:'):
        from .models import GitRepository
        repo=db.get(GitRepository,target[len('repository:'):])
        return {target,'repositories'} if repo else {target}
    resource = db.get(Resource, target)
    result={item.id for item in rbac.ancestors(db, resource)} if resource else {target}
    if resource:
        collections={'kafka':'kafka','topic':'kafka','source':'storage','schema':'storage','api':'apis','api-operation':'apis',
            'api-group':'apis','service':'set-services' if resource.config.get('service_account') else 'workloads'}
        if resource.kind in collections:
            result.add(collections[resource.kind])
    return result


def scope_matches(granted, actual):
    return granted == actual or (granted == "*" and actual is not None)


@cached
def allowed(db, user, action: str, target: str, layer_id: Optional[str] = None) -> bool:
    if action not in REGISTRY or not target:
        return False
    user = rbac.resolve_user(db, user)
    actual_layer = resource_layer(db, target)
    resource = db.get(Resource, target)
    global_package = ((resource is not None and resource.kind in ('package-root', 'package')) or target.startswith(('repository:','secret:','git-credential:'))) and not actual_layer
    if actual_layer and layer_id and actual_layer != layer_id:
        return False
    layer = actual_layer or layer_id or (user.environment_id if global_package else None)
    if user.environment_id and layer != user.environment_id:
        return False
    if acting_as_admin(user):
        return True
    chain = targets(db, target)
    held_roles = set()
    for grant in grants(db, user):
        if grant.target_id not in chain or not (scope_matches(grant.layer_id, layer) or
            (global_package and grant.layer_id is None)):
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


def domain_allowed(db, user, action, resource, legacy_action=None):
    """Named function permission, with explicit compatibility for existing resource roles."""
    if resource is None:
        return False
    if allowed(db,user,action,resource.id):
        return True
    if not legacy_action or action not in REGISTRY or not rbac.allowed(db,user,legacy_action,resource,named=False):
        return False
    # Domain roles use the same action bundles as named roles. Generic legacy
    # verbs (e.g. edit) must not make Contributor an Executor or Manager.
    aliases={'Reader':{'Executor'},'Consumer':{'Executor'},'Publisher':{'Contributor','Executor','Manager'},
        'Maintainer':{'Contributor','Executor','Manager'}}
    held=set()
    for grant in rbac.access(db,user,resource):
        role=grant['role']
        held.update(aliases.get(role,{role}))
    definition=REGISTRY[action]
    return bool(held & set(definition['roles'])) or bool(definition.get('requires_roles') and set(definition['requires_roles']).issubset(held))


def require_domain(db, user, action, resource, legacy_action=None):
    if not domain_allowed(db, user, action, resource, legacy_action):
        raise HTTPException(403, f'{action} permission required')


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
