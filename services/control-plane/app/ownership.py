"""Primary group ownership, secondary group access, and protected global administration."""
import re

from fastapi import HTTPException
from sqlalchemy import case, delete, func, select, text

from .config import get_settings
from .authorization_cache import cached
from .models import (AppGroup, AuditEvent, CatalogAsset, ComputePool, DirectoryPrincipal, GitRepository, IdentityBinding,
                     Resource, RoleAssignment, ServiceRoleAssignment, UserPreference, Workspace)

GLOBAL_ADMIN = "000000000001"


def ensure_global_admin(db):
    from .rbac import principal
    group = db.get(AppGroup, GLOBAL_ADMIN)
    if group is None:
        if db.scalar(select(AppGroup).where(AppGroup.name == "Global Admin")):
            raise RuntimeError("Global Admin is reserved for the system group; rename the existing custom group first")
        group = AppGroup(id=GLOBAL_ADMIN, name="Global Admin", owners=[],
            description="Protected system group. Global administration and access to every object. Execution layer boundaries still apply.")
        db.add(group); db.flush()
    role = principal(db, "role", get_settings().admin_role)
    if not db.scalar(select(IdentityBinding.id).where(IdentityBinding.app_group_id == GLOBAL_ADMIN,
                                                   IdentityBinding.principal_id == role.id)):
        db.add(IdentityBinding(app_group_id=GLOBAL_ADMIN, principal_id=role.id)); db.flush()
    return group


def available_groups(db, user):
    from .rbac import closure
    if user.execution_context or user.username.startswith(("service:", "sa:")):
        return []
    ids = closure(db, user.principal_id)
    member = set(db.scalars(select(IdentityBinding.app_group_id).where(IdentityBinding.principal_id.in_(ids))))
    return [g for g in db.scalars(select(AppGroup).order_by(AppGroup.name))
            if g.id in member or (user.is_admin and g.id == GLOBAL_ADMIN)]


def selected_group_id(db, user):
    """A persisted selection is never silently replaced if membership is revoked."""
    if user.execution_context or user.username.startswith(("service:", "sa:")):
        from .models import Workload, Run
        kind, _, ident = user.execution_context.partition(":")
        if kind == "run":
            run = db.get(Run, ident)
            kind, ident = ("workspace", run.workspace_id) if run and run.workspace_id else ("workload", run.workload_id if run else None)
        if kind == "workspace":
            ws = db.get(Workspace, ident)
            return ws.owner_group_id or "" if ws else ""
        if kind == "workload":
            pub = db.get(Workload, ident)
            resource = db.get(Resource, pub.resource_id) if pub else None
        else:
            principal = db.get(DirectoryPrincipal, user.principal_id)
            resource = db.scalar(select(Resource).where(Resource.object_id == principal.external_id)) if principal else None
        return (resource.config or {}).get("owner_group_id", "") if resource else ""
    preference = db.get(UserPreference, user.principal_id)
    if preference:
        return preference.active_group_id or ""
    if user.is_admin:
        return GLOBAL_ADMIN
    from .rbac import closure
    ids = closure(db, user.principal_id)
    return db.scalar(select(AppGroup.id).join(IdentityBinding, IdentityBinding.app_group_id == AppGroup.id)
                     .outerjoin(RoleAssignment,RoleAssignment.app_group_id == AppGroup.id)
                     .where(IdentityBinding.principal_id.in_(ids)).group_by(AppGroup.id,AppGroup.name)
                     .order_by(func.max(case((RoleAssignment.role.in_(("Owner","Admin")),1),else_=0)).desc(),AppGroup.name).limit(1)) or ""


def owner_group(db, user):
    from .rbac import resolve_user
    user = resolve_user(db, user,refresh=not bool(user.principal_id))
    ident = user.active_group_id
    if not ident:
        raise HTTPException(422, "Select an active app group before creating an object. Ask an administrator to add you to a group if none are available.")
    group = db.get(AppGroup, ident)
    if not group:
        raise HTTPException(422, "The active group no longer exists. Select another group.")
    if not user.execution_context and ident not in {g.id for g in available_groups(db, user)}:
        raise HTTPException(403, "You no longer have access to the active group. Select another group.")
    return group


def require_create(db, user, parent=None, action="create"):
    """CREATE uses only the primary group, never direct or secondary grants."""
    from . import rbac
    from .environments import permits
    user = rbac.resolve_user(db, user,refresh=not bool(user.principal_id))
    if parent and not permits(db, user, parent):
        raise HTTPException(403, "cross-layer creation is forbidden")
    if parent:
        rbac.require(db,user,action,parent)
    group = owner_group(db, user)
    if group.id == GLOBAL_ADMIN and user.is_admin and not user.execution_context:
        return group
    if parent:
        chain = {r.id for r in rbac.ancestors(db, parent)}
        grants = db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == group.id,
                                                        RoleAssignment.resource_id.in_(chain)))
        if any(action in rbac.roles_for(parent).get(grant.role, set()) for grant in grants):
            return group
    raise HTTPException(403, f"Active group {group.name!r} needs {action} permission here. Secondary groups cannot authorize creation.")


@cached
def _owner_group_record(db, ident):
    return db.get(AppGroup, ident)


def owner_info(db, resource):
    ident = (resource.config or {}).get("owner_group_id")
    group = _owner_group_record(db, ident) if ident else None
    return {"owner_group_id": group.id if group else None, "owner_group_name": group.name if group else None}


def browsing_scope(db, user):
    """Interactive discovery follows the primary group; execution still uses combined grants."""
    from . import rbac
    from .environments import permits
    user = rbac.resolve_user(db, user)
    key = ("browsing_scope", user.principal_id, user.active_group_id, user.environment_id, user.execution_context)
    if key in db.info:
        return db.info[key]
    resources = {r.id:r for r in db.scalars(select(Resource))}
    interactive = not user.execution_context and not user.username.startswith(("service:", "sa:"))
    if not interactive:
        # Service and editor credentials have no interactive selection to filter by.
        result = {r.id:{g["role"] for g in rbac.access(db,user,r)} for r in resources.values()}
    elif not user.active_group_id or user.active_group_id not in {g.id for g in available_groups(db,user)}:
        result = {}
    elif user.active_group_id == GLOBAL_ADMIN and user.is_admin:
        result = {r.id:{"Owner"} for r in resources.values() if permits(db,user,r)}
    else:
        grants = {}
        for g in rbac.group_grants(db, {user.active_group_id}, user):  # includes what data product roles give
            order = list(rbac.roles_for(resources[g.resource_id])) if g.resource_id in resources else []
            current = grants.get(g.resource_id)
            if current is None or (g.role in order and current in order and order.index(g.role) > order.index(current)):
                grants[g.resource_id] = g.role
        result = {}
        for resource in resources.values():
            if not permits(db,user,resource):
                continue
            roles, seen, current = set(), set(), resource
            while current:
                if current.id in seen:
                    raise HTTPException(409,"resource cycle detected")
                seen.add(current.id)
                if current.id in grants:
                    roles.add(grants[current.id])
                current = resources.get(current.parent_id)
            if roles:
                result[resource.id] = roles
    db.info[key] = result
    return result


def browsing_allowed(db, user, resource):
    from .rbac import roles_for
    return bool(resource and any("read" in roles_for(resource).get(role,set())
                                 for role in browsing_scope(db,user).get(resource.id,set())))


def creation_data_snapshot(db,user):
    """Primary-group CREATE authority; service identities keep their direct data grants."""
    from . import rbac
    from .data_access import snapshot
    from .environments import permits
    from .models import DataSource
    user = rbac.resolve_user(db,user)
    if user.username.startswith(("service:","sa:")) or (user.is_admin and not user.execution_context and user.active_group_id == GLOBAL_ADMIN):
        return snapshot(db,user)
    if not user.active_group_id or user.active_group_id not in rbac.app_groups(db,user):
        return {}
    grants = {g.resource_id:g.role for g in db.scalars(select(RoleAssignment).where(
        RoleAssignment.app_group_id == user.active_group_id,RoleAssignment.role.in_(("Admin","Owner"))))}
    result = {}
    for source in db.scalars(select(DataSource)):
        root = rbac.source_resource(db,source)
        if not permits(db,user,root):
            continue
        schemas = {r.name:grants.get(r.id) or grants.get(root.id) for r in db.scalars(select(Resource).where(
            Resource.kind == "schema",Resource.parent_id == root.id)) if r.id in grants or root.id in grants}
        if schemas:
            result[source.id] = schemas
    return result


def migrate(db):
    """Retire generated personal groups without broadening non-admin resource grants."""
    from . import rbac
    ensure_global_admin(db)
    if db.execute(text("SELECT revision FROM fdp_migrations WHERE revision='003_active_groups'")).first():
        return
    retired = 0
    replacements = {}
    for group in list(db.scalars(select(AppGroup))):
        if not re.fullmatch(r"Personal owners [a-f0-9]{12}", group.name):
            continue
        bindings = list(db.scalars(select(IdentityBinding).where(IdentityBinding.app_group_id == group.id)))
        principals = [db.get(DirectoryPrincipal, b.principal_id) for b in bindings]
        if any(p and p.kind not in ("user", "service") for p in principals):
            # A manually extended group has ongoing IdP bindings that direct user grants
            # cannot preserve. Keep that security policy as an ordinary named group.
            group.name = "Legacy ownership " + group.id
            continue
        admin_owned = bool(principals) and all(p and p.kind == "user" and any(
            role and role.kind == "role" and role.name == get_settings().admin_role
            for role in (db.get(DirectoryPrincipal, pid) for pid in rbac.closure(db, p.id))) for p in principals)
        replacements[group.id] = GLOBAL_ADMIN if admin_owned else None
        for grant in list(db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == group.id))):
            resource = db.get(Resource, grant.resource_id)
            if admin_owned:
                existing = db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == resource.id,
                                                                 RoleAssignment.app_group_id == GLOBAL_ADMIN))
                if not existing:
                    db.add(RoleAssignment(resource_id=resource.id, app_group_id=GLOBAL_ADMIN, role=grant.role))
                elif set(rbac.roles_for(resource)[existing.role]) < set(rbac.roles_for(resource)[grant.role]):
                    existing.role=grant.role
            else:
                for principal in principals:
                    if not principal:
                        continue
                    direct = db.scalar(select(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id == resource.id,
                                                                          ServiceRoleAssignment.principal_id == principal.id))
                    if not direct:
                        db.add(ServiceRoleAssignment(resource_id=resource.id, principal_id=principal.id, role=grant.role))
                    elif set(rbac.roles_for(resource)[direct.role]) < set(rbac.roles_for(resource)[grant.role]):
                        direct.role = grant.role
            db.delete(grant)
        db.flush()
        for asset in db.scalars(select(CatalogAsset)):
            if (asset.governance or {}).get("owner_group_id") == group.id:
                asset.governance = {**asset.governance, "owner_group_id": replacements[group.id]}
        db.execute(delete(IdentityBinding).where(IdentityBinding.app_group_id == group.id))
        db.execute(delete(UserPreference).where(UserPreference.active_group_id == group.id))
        for model in (Workspace,GitRepository,ComputePool):
            for obj in db.scalars(select(model).where(model.owner_group_id == group.id)):
                obj.owner_group_id=replacements[group.id]
        db.flush()
        db.delete(group); retired += 1
    db.flush()
    for resource in db.scalars(select(Resource)):
        old = (resource.config or {}).get("owner_group_id")
        if old in replacements:
            resource.config = {**resource.config, "owner_group_id": replacements[old]}
        if not resource.config.get("owner_group_id"):
            chain = [r.id for r in rbac.ancestors(db,resource)]
            owners = list(db.scalars(select(RoleAssignment).where(RoleAssignment.resource_id.in_(chain),
                                                                  RoleAssignment.role == "Owner")))
            if owners:
                preferred = next((g for g in owners if g.app_group_id != GLOBAL_ADMIN), owners[0])
                resource.config = {**resource.config, "owner_group_id": preferred.app_group_id}
    for ws in db.scalars(select(Workspace)):
        if ws.owner_group_id in replacements:
            ws.owner_group_id = replacements[ws.owner_group_id]
        if not ws.owner_group_id:
            principal = db.get(DirectoryPrincipal,ws.principal_id) if ws.principal_id else None
            if principal and principal.active:
                from .auth import User
                actor = rbac.resolve_user(db,User(principal.name,principal_id=principal.id))
                ws.owner_group_id = actor.active_group_id or None
    for model in (GitRepository,ComputePool):
        for obj in db.scalars(select(model)):
            if not obj.owner_group_id:
                obj.owner_group_id=GLOBAL_ADMIN
    db.add(AuditEvent(actor="system", action="migration.active_groups", target="003_active_groups",
                      detail={"personal_groups_retired": retired}))
    db.execute(text("INSERT INTO fdp_migrations(revision) VALUES ('003_active_groups')"))
    db.flush()
