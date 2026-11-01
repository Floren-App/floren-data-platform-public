"""App groups, resource trees, fixed assignments and effective access."""
from .. import layers

from typing import List, Optional, Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict, model_validator
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from ..auth import User
from ..db import get_db
from ..deps import get_actor, get_secret_store
from ..models import (AppGroup, DirectoryPrincipal, DirectoryMembership, IdentityBinding, Resource, RoleAssignment, ServiceRoleAssignment,
                      DataSource, Workload, ServiceAccount, utcnow)
from .. import rbac
from ..ownership import GLOBAL_ADMIN, require_create, owner_info, browsing_scope, browsing_allowed
from .common import audit

router = APIRouter(prefix="/v1/rbac", tags=["RBAC"])

class GroupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=500)
    owners: List[str] = Field(min_length=1)
    app_profile: Optional[str] = Field(default=None, max_length=64,
                                       description="Name of the app profile the group gets; empty: the default profile")

class BindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: str

class ResourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    parent_id: str = "workloads"
    layer_id: Optional[str] = None
    environment: bool = False
    workspaces: bool = False
    branch: str = ""

class SchemaIn(BaseModel):
    prefix: Optional[str] = Field(default=None, max_length=1024)
    append_only: bool = False
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)

class AssignmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_group_id: Optional[str] = None
    principal_id: Optional[str] = None
    role: str
    justification_id: Optional[str] = Field(default=None, max_length=12,
        description="An approved compliance case for this access, when an access policy applies (API and Terraform)")

    @model_validator(mode="after")
    def one_recipient(self):
        if bool(self.app_group_id) == bool(self.principal_id):
            raise ValueError("choose exactly one app group or service identity")
        return self


def current(db, user):
    return rbac.resolve_user(db, user, refresh=not bool(user.principal_id))


def admin(db, user):
    user = current(db, user)
    if not user.is_admin or user.execution_context:
        raise HTTPException(403, "interactive platform admin required")
    return user


def _set_profile(db, group, name):
    """The group's app profile, by name ("" or None for the default profile)."""
    from ..models import AppProfile
    if not name:
        group.profile_id = None
        return
    profile = db.scalar(select(AppProfile).where(AppProfile.name == name))
    if profile is None:
        raise HTTPException(422, f"no app profile named {name}")
    group.profile_id = profile.id


def group_out(db, g, user):
    from ..models import AppProfile
    profile = db.get(AppProfile, g.profile_id) if g.profile_id else None
    return {"id": g.id, "name": g.name, "description": g.description, "owners": g.owners, "origin": "app",
            "app_profile": profile.name if profile else None,
            "system":g.id == GLOBAL_ADMIN,
            "protected_bindings":[p.id for p in db.scalars(select(DirectoryPrincipal).where(
                DirectoryPrincipal.kind == "role",DirectoryPrincipal.name == user.admin_role))] if g.id == GLOBAL_ADMIN else [],
            "can_manage": user.is_admin or (g.id != GLOBAL_ADMIN and user.principal_id in g.owners),
            "bindings": list(db.scalars(select(IdentityBinding.principal_id).where(
                IdentityBinding.app_group_id == g.id)))}


def resource_out(db, r, user):
    out = {"id": r.id, "name": r.name, "kind": r.kind, "parent_id": r.parent_id,
           "object_id": r.object_id, "config": r.config or {},
           **layers.info(db, r),
           **owner_info(db,r),
           "actions": [a for a in {a for v in rbac.roles_for(r).values() for a in v}
                       if rbac.allowed(db, user, a, r)]}
    if r.kind in ("source", "schema"):
        # A data role on the object or a parent, as in data_scope: no platform-admin bypass,
        # and a source listed only to reach an authorized schema does not count.
        out["data_access"] = bool(browsing_scope(db,user).get(r.id,set()) & rbac.DATA_ROLES.keys())
    return out


def get_resource(db, resource_id):
    r = db.get(Resource, resource_id)
    if not r:
        raise HTTPException(404, "resource not found")
    return r


@router.get("/roles")
def roles(_: User = Depends(get_actor)):
    return {"data": {k: sorted(v) for k, v in rbac.DATA_ROLES.items()},
            "program": {k: sorted(v) for k, v in rbac.PROGRAM_ROLES.items()},
            "api": {k: sorted(v) for k, v in rbac.API_ROLES.items()},
            "package": {k: sorted(v) for k, v in rbac.PACKAGE_ROLES.items()},
            "product": {k: sorted(v) for k, v in rbac.PRODUCT_ROLES.items()}}


@router.get("/directory")
def directory(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = current(db, user)
    # Group owners need a directory picker to bind identities; ordinary users cannot enumerate it.
    if not user.is_admin and not any(user.principal_id in g.owners for g in db.scalars(select(AppGroup))):
        raise HTTPException(403, "group owner required")
    principals = list(db.scalars(select(DirectoryPrincipal).order_by(DirectoryPrincipal.name)))
    memberships = list(db.scalars(select(DirectoryMembership)))
    groups = list(db.scalars(select(AppGroup)))
    bindings = list(db.scalars(select(IdentityBinding)))
    own_groups = rbac.app_groups(db, user)
    inspectable = {g.id for g in groups if user.is_admin or user.principal_id in g.owners or g.id in own_groups}
    result = []
    active_ids = {p.id for p in principals if p.active}
    for p in principals:
        ancestors = {p.id}
        while True:
            expanded = ancestors | {m.group_id for m in memberships if m.member_id in ancestors and m.group_id in active_ids}
            if expanded == ancestors:
                break
            ancestors = expanded
        app_groups = {b.app_group_id for b in bindings if b.principal_id in ancestors and b.app_group_id in inspectable}
        origin = "app" if p.kind == "service" or p.provider == "local" else "idp"
        result.append({"id": p.id, "kind": p.kind, "name": p.name, "active": p.active,
            "provider": p.provider, "external_id": p.external_id, "managed": p.managed,
            "origin": origin, "provisioning": "app" if origin == "app" else "scim" if p.managed else "oidc",
            "display_name": p.attributes.get("displayName") or (" ".join(str(v) for v in p.attributes["name"].values()) if isinstance(p.attributes.get("name"), dict) else "") or p.name,
            "member_of": [m.group_id for m in memberships if m.member_id == p.id],
            "members": [m.member_id for m in memberships if m.group_id == p.id],
            "app_groups": sorted(app_groups),
            "platform_admin": p.active and p.kind == "user" and (GLOBAL_ADMIN in app_groups or any(x.kind == "role" and x.name == user.admin_role and x.id in ancestors for x in principals))})
    return result


@router.get("/groups")
def groups(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = current(db, user)
    memberships = rbac.app_groups(db, user)
    result = []
    for g in db.scalars(select(AppGroup).order_by(AppGroup.name)):
        out = group_out(db, g, user)
        out["can_inspect"] = out["can_manage"] or g.id in memberships
        if not out["can_inspect"]:
            out["owners"], out["bindings"] = [], []
        result.append(out)
    return result


@router.get("/groups/{group_id}/assignments")
def group_assignments(group_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = current(db, user)
    group = db.get(AppGroup, group_id)
    if not group:
        raise HTTPException(404, "group not found")
    if not user.is_admin and user.principal_id not in group.owners and group_id not in rbac.app_groups(db, user):
        raise HTTPException(403, "group membership or ownership required")
    result = []
    for grant in db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == group_id)):
        resource = db.get(Resource, grant.resource_id)
        if resource and rbac.allowed(db, user, "read", resource):
            result.append({"resource": resource_out(db, resource, user), "role": grant.role})
    return result


# --- effective access: what a group, person or service identity can reach -----------------------------------

def access_rows(db, viewer, assigned: list, names: dict, held: set = frozenset()) -> List[dict]:
    """Roles held directly and through data products, with end dates and deadlines from access policies.
    names: group ID -> name (for "via"). held: (resource, group) still waiting for membership approval."""
    from ..models import CatalogProduct, JustificationDeadline, JustifiedGrant
    rows = []
    for a in assigned + rbac.product_grants(db, [x for x in assigned if isinstance(x, RoleAssignment)]):
        resource = db.get(Resource, a.resource_id)
        if resource is None or not rbac.allowed(db, viewer, "read", resource):
            continue
        group_id = getattr(a, "app_group_id", None)
        row = {"resource": resource_out(db, resource, viewer), "role": a.role,
               "group": {"id": group_id, "name": names.get(group_id, group_id)} if group_id else None,
               "via": "product" if isinstance(a, rbac.ProductGrant) else "direct", "product": None, "until": None, "deadline": None,
               "pending_membership": (a.resource_id, group_id) in held}
        if isinstance(a, rbac.ProductGrant):
            product = db.scalar(select(CatalogProduct).where(CatalogProduct.access_resource_id == a.product_resource_id))
            row["product"] = {"id": product.id, "title": product.title} if product else None
        elif group_id:
            grant = db.scalar(select(JustifiedGrant).where(JustifiedGrant.resource_id == a.resource_id, JustifiedGrant.app_group_id == group_id))
            deadline = db.scalar(select(JustificationDeadline).where(JustificationDeadline.resource_id == a.resource_id,
                                                                     JustificationDeadline.app_group_id == group_id))
            row["until"] = grant.until if grant else None
            row["deadline"] = deadline.due_at if deadline else None
        rows.append(row)
    rows.sort(key=lambda r: (r["resource"]["kind"] == "product", r["via"] == "product", r["resource"]["name"].lower()))
    return rows


def compliance_rows(db, group_ids) -> List[dict]:
    from ..compliance import ROLES
    from ..models import ComplianceRole
    out = []
    for item in db.scalars(select(ComplianceRole).where(ComplianceRole.app_group_id.in_(list(group_ids)))):
        group = db.get(AppGroup, item.app_group_id)
        out.append({"role": item.role, "label": ROLES.get(item.role, item.role), "scope": item.scope or {},
                    "group": {"id": item.app_group_id, "name": group.name if group else item.app_group_id}})
    return out


@router.get("/groups/{group_id}/access")
def group_access(group_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """What the group can reach: roles on objects (directly and through data products), compliance roles and its
    app profile. For members, owners and administrators."""
    from ..models import AppProfile
    user = current(db, user)
    group = db.get(AppGroup, group_id)
    if not group:
        raise HTTPException(404, "group not found")
    if not user.is_admin and user.principal_id not in group.owners and group_id not in rbac.app_groups(db, user):
        raise HTTPException(403, "group membership or ownership required")
    assigned = list(db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == group_id)))
    profile = db.get(AppProfile, group.profile_id) if group.profile_id else None
    return {"system": group_id == GLOBAL_ADMIN, "grants": access_rows(db, user, assigned, {group.id: group.name}),
            "compliance_roles": compliance_rows(db, [group_id]), "app_profile": profile.name if profile else None}


@router.get("/principals/{principal_id}/access")
def principal_access(principal_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """What a person or service identity can reach, and through which group. For administrators and the
    identity itself."""
    from types import SimpleNamespace
    from ..compliance import unapproved_justified
    user = current(db, user)
    principal = db.get(DirectoryPrincipal, principal_id)
    if principal is None or principal.kind not in ("user", "service"):
        raise HTTPException(404, "person or service identity not found")
    if not user.is_admin and user.principal_id != principal_id:
        raise HTTPException(403, "administrators, or the identity itself")
    ids = rbac.closure(db, principal_id)
    groups = []
    for binding in db.scalars(select(IdentityBinding).where(IdentityBinding.principal_id.in_(list(ids)))):
        group = db.get(AppGroup, binding.app_group_id)
        if group is None or any(g["id"] == group.id for g in groups):
            continue
        via = db.get(DirectoryPrincipal, binding.principal_id)
        groups.append({"id": group.id, "name": group.name, "system": group.id == GLOBAL_ADMIN,
                       "via": None if binding.principal_id == principal_id else (via.name if via else binding.principal_id)})
    groups.sort(key=lambda g: g["name"].lower())
    group_ids = [g["id"] for g in groups]
    assigned = list(db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id.in_(group_ids)))) if group_ids else []
    direct = list(db.scalars(select(ServiceRoleAssignment).where(ServiceRoleAssignment.principal_id == principal_id)))
    held = unapproved_justified(db, SimpleNamespace(principal_id=principal_id), group_ids)
    return {"principal": {"id": principal.id, "name": principal.name, "kind": principal.kind, "active": principal.active},
            "groups": groups, "grants": access_rows(db, user, assigned + direct, {g["id"]: g["name"] for g in groups}, held),
            "compliance_roles": compliance_rows(db, group_ids)}


def check_owners(db, owners):
    for owner in owners:
        p = db.get(DirectoryPrincipal, owner)
        if not p or p.kind != "user" or not p.active:
            raise HTTPException(422, "group owners must be active directory users")


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = admin(db, user)
    require_create(db,user)
    if body.name.strip().casefold() == "global admin":
        raise HTTPException(409,"Global Admin is a protected system group")
    check_owners(db, body.owners)
    if db.scalar(select(AppGroup).where(AppGroup.name == body.name)):
        raise HTTPException(409, "group name already exists")
    group = AppGroup(**body.model_dump(exclude={"app_profile"}))
    _set_profile(db, group, body.app_profile)
    db.add(group)
    db.flush()
    audit(db, user.username, "app_group.create", group.id, owners=body.owners)
    db.commit()
    return group_out(db, group, user)


@router.put("/groups/{group_id}")
def update_group(group_id: str, body: GroupIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = admin(db, user)
    group = db.get(AppGroup, group_id)
    if not group:
        raise HTTPException(404, "group not found")
    if group.id == GLOBAL_ADMIN or body.name.strip().casefold() == "global admin":
        raise HTTPException(409,"Global Admin is protected; manage its identity bindings instead")
    check_owners(db, body.owners)
    duplicate = db.scalar(select(AppGroup).where(AppGroup.name == body.name, AppGroup.id != group.id))
    if duplicate:
        raise HTTPException(409, "group name already exists")
    before = group.owners
    group.name, group.description, group.owners = body.name, body.description, body.owners
    before_profile = group.profile_id
    if "app_profile" in body.model_fields_set:  # left out: unchanged
        _set_profile(db, group, body.app_profile)
    audit(db, user.username, "app_group.update", group.id, before_owners=before, owners=body.owners,
          **({"app_profile": body.app_profile or "default"} if group.profile_id != before_profile else {}))
    db.commit()
    return group_out(db, group, user)


@router.delete("/groups/{group_id}", status_code=204)
def delete_group(group_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = admin(db, user)
    group = db.get(AppGroup, group_id)
    if not group:
        raise HTTPException(404, "group not found")
    if group.id == GLOBAL_ADMIN:
        raise HTTPException(409,"Global Admin cannot be deleted")
    if any((resource.config or {}).get('owner_group_id') == group.id for resource in db.scalars(select(Resource))):
        raise HTTPException(409,"this group still owns objects")
    from ..models import Workspace, GitRepository, ComputePool, UserPreference
    if any(db.scalar(select(model.id).where(model.owner_group_id == group.id).limit(1))
           for model in (Workspace,GitRepository,ComputePool)):
        raise HTTPException(409,"transfer this group's object ownership before deleting it")
    if db.scalar(select(RoleAssignment).where(RoleAssignment.app_group_id == group.id)):
        raise HTTPException(409, "remove this group's resource assignments before deleting it")
    db.execute(delete(IdentityBinding).where(IdentityBinding.app_group_id == group.id))
    for preference in db.scalars(select(UserPreference).where(UserPreference.active_group_id == group.id)):
        preference.active_group_id = None
    db.flush()
    db.delete(group)
    audit(db, user.username, "app_group.delete", group_id)
    db.commit()


def managed_group(db, user, group_id):
    user = current(db, user)
    g = db.get(AppGroup, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    if user.execution_context or (not user.is_admin and (g.id == GLOBAL_ADMIN or user.principal_id not in g.owners)):
        raise HTTPException(403, "designated group owner required")
    return g, user


@router.post("/groups/{group_id}/bindings", status_code=201)
def bind(group_id: str, body: BindingIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    group, user = managed_group(db, user, group_id)
    p = db.get(DirectoryPrincipal, body.principal_id)
    if not p or not p.active:
        raise HTTPException(422, "active directory principal required")
    if group.id == GLOBAL_ADMIN and p.kind == "service":
        raise HTTPException(422,"Global Admin is reserved for human administrative identities")
    binding = db.scalar(select(IdentityBinding).where(IdentityBinding.app_group_id == group.id,
                                                    IdentityBinding.principal_id == p.id))
    if binding is None:
        db.add(IdentityBinding(app_group_id=group.id, principal_id=p.id))
    audit(db, user.username, "app_group.bind", group.id, principal_id=p.id)
    db.commit()
    return group_out(db, group, user)


@router.delete("/groups/{group_id}/bindings/{principal_id}", status_code=204)
def unbind(group_id: str, principal_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    group, user = managed_group(db, user, group_id)
    p = db.get(DirectoryPrincipal,principal_id)
    if group.id == GLOBAL_ADMIN and p and p.kind == "role" and p.name == user.admin_role:
        raise HTTPException(409,"the configured IdP administrator role remains bound to Global Admin")
    db.execute(delete(IdentityBinding).where(IdentityBinding.app_group_id == group.id,
                                           IdentityBinding.principal_id == principal_id))
    audit(db, user.username, "app_group.unbind", group.id, principal_id=principal_id)
    db.commit()


@router.get("/resources")
def resources(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = current(db, user)
    rbac.root(db)
    rbac.api_root(db)
    rbac.packages_root(db)
    for ds in db.scalars(select(DataSource)):
        rbac.source_resource(db, ds)
    for sa in db.scalars(select(ServiceAccount)):
        rbac.service_account_resource(db, sa)
    workloads = list(db.scalars(select(Workload).where(Workload.status != "stopped")))
    workload_resources = {rbac.workload_resource(db, pub).id for pub in workloads}
    db.commit()
    # Retain old IAM records for history, but never expose retired/missing workloads
    # as navigable objects. Packages have their own pages (and can be many). Service accounts have their own lifecycle and remain manageable.
    registry = list(db.scalars(select(Resource)))
    all_resources = [r for r in registry
                     if r.kind not in ("layer", "package", "topic") and not (r.kind == "kafka" and r.config.get("deleted")) and (r.kind != "service" or r.config.get("service_account") or r.id in workload_resources)]
    visible = {r.id for r in all_resources if browsing_allowed(db, user, r)}
    # Kafka topics load lazily, but topic-only grants must still reveal the cluster
    # name needed to navigate to them. No child topic metadata enters the hub payload.
    visible.update(t.parent_id for t in registry
                   if t.kind == 'topic' and not t.config.get('deleted') and browsing_allowed(db, user, t))
    # Show only names of ancestors required to navigate an authorized descendant.
    for r in all_resources:
        if r.id in visible:
            visible.update(x.id for x in rbac.ancestors(db, r))
    return [resource_out(db, r, user) for r in all_resources if r.id in visible]


@router.post("/folders", status_code=201)
def create_folder(body: ResourceIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = current(db, user)
    parent = get_resource(db, body.parent_id)
    if parent.kind not in ("root", "folder"):
        raise HTTPException(422, "folders belong under Deployed or another folder")
    require_create(db,user,parent)
    if db.scalar(select(Resource).where(Resource.parent_id == parent.id, Resource.name == body.name)):
        raise HTTPException(409, "name already exists in folder")
    config = {}
    if body.environment:
        from ..environments import resource_environment
        if not user.is_admin or user.execution_context:
            raise HTTPException(403, "only interactive platform admins create environments")
        if resource_environment(db, parent) or not body.branch:
            raise HTTPException(422, "environments cannot nest and require a branch")
        config = {"environment": True, "workspaces": body.workspaces, "branch": body.branch}
    if body.layer_id:
        config["layer_id"] = layers.selection(db, body.layer_id, parent)
    elif body.environment:
        config["layer_id"] = layers.legacy_layer(db, body.branch in ("main", "master", "prod"))
    folder = Resource(kind="folder", name=body.name, parent_id=parent.id, config=config)
    db.add(folder)
    db.flush()
    rbac.own(db,user,folder)
    audit(db, user.username, "folder.create", folder.id, parent=parent.id)
    db.commit()
    return resource_out(db, folder, user)


@router.patch("/resources/{resource_id}")
def move_resource(resource_id: str, body: ResourceIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    r = get_resource(db, resource_id)
    user = current(db, user)
    if r.kind not in ("folder", "service") or r.config.get("service_account"):
        raise HTTPException(422, "only folders and workloads can move")
    parent = get_resource(db, body.parent_id)
    if parent.kind not in ("root", "folder") or r.id in {x.id for x in rbac.ancestors(db, parent)}:
        raise HTTPException(422, "invalid parent or folder cycle")
    rbac.require(db, user, "delete", get_resource(db, r.parent_id))
    rbac.require(db, user, "create", parent)
    if db.scalar(select(Resource).where(Resource.parent_id == parent.id, Resource.name == body.name, Resource.id != r.id)):
        raise HTTPException(409, "name already exists in folder")
    from ..environments import resource_environment
    if r.config.get("environment") or (layers.explicit_layer(db, parent) and resource_environment(db, r) != resource_environment(db, parent)):
        raise HTTPException(409, "environment boundaries are immutable; deploy a separate instance")
    old = r.parent_id
    if parent.id != old:
        # Moving can grant inherited Owner to new people. Treat a change in inherited
        # assignments as permission management, not a way for Admin to promote itself.
        old_chain = {x.id for x in rbac.ancestors(db, get_resource(db, old))}
        new_chain = {x.id for x in rbac.ancestors(db, parent)}
        def inherited_grants(ids):
            return ({("group", a.app_group_id, a.role) for a in db.scalars(select(RoleAssignment).where(RoleAssignment.resource_id.in_(ids)))} |
                    {("service", a.principal_id, a.role) for a in db.scalars(select(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id.in_(ids)))})
        if inherited_grants(old_chain) != inherited_grants(new_chain):
            rbac.require(db, user, "assign", r)
    # A folder can have inherited its boundary from the old parent. Moving it
    # into an organization folder must not remove that execution boundary.
    old_layer = resource_environment(db, r)
    if old_layer:
        r.config = {**r.config, "layer_id": old_layer}
    r.parent_id, r.name = parent.id, body.name
    audit(db, user.username, "resource.move", r.id, before_parent=old, parent=parent.id)
    db.commit()
    return resource_out(db, r, user)


@router.delete("/folders/{resource_id}", status_code=204)
def delete_folder(resource_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    r = get_resource(db, resource_id)
    if r.kind != "folder":
        raise HTTPException(422, "only folders can be deleted here")
    rbac.require(db, user, "delete", r)
    from ..models import Workload
    children = list(db.scalars(select(Resource).where(Resource.parent_id == r.id)))
    retired = [c for c in children if c.kind == "service" and not c.config.get("service_account")
               and (pub := db.get(Workload, c.object_id)) is not None and pub.status == "stopped"]
    if len(retired) != len(children):
        raise HTTPException(409, "folder must be empty")
    # Undeployed programs keep their entry for run and catalog history; it moves up a level.
    for child in retired:
        child.parent_id = r.parent_id
    if r.config.get("environment"):
        from ..models import Workspace
        from ..environments import resource_environment
        if db.scalar(select(Workspace.id).where(Workspace.environment_id == r.id)) or any(
                x.id != r.id and resource_environment(db, x) == r.id for x in db.scalars(select(Resource))):
            raise HTTPException(409, "environment still contains workspaces, data or APIs")
    rbac.delete_resource_assignments(db, r.id)
    db.delete(r)
    audit(db, user.username, "folder.delete", resource_id)
    db.commit()


@router.post("/sources/{name}/schemas", status_code=201)
def register_schema(name: str, body: SchemaIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = current(db, user)
    ds = db.scalar(select(DataSource).where(DataSource.name == name))
    if not ds:
        raise HTTPException(404, "source not found")
    parent = rbac.source_resource(db, ds)
    require_create(db,user,parent,action="assign")
    if db.scalar(select(Resource).where(Resource.parent_id == parent.id, Resource.name == body.name)):
        raise HTTPException(409, "schema already registered")
    config = {}
    from ..config import get_settings
    from ..sql_access import SQL_KINDS, catalog_name
    if body.append_only:
        if get_settings().query_executor != 'trino' or ds.kind not in SQL_KINDS:
            raise HTTPException(422, 'append-only schemas require a governed Trino SQL source')
        config['append_only'] = True
    if get_settings().query_executor == "trino" and ds.kind in SQL_KINDS:
        if body.name in {"information_schema", "system"} or (ds.kind == "postgres" and body.name.startswith("pg_")):
            raise HTTPException(422, "system schemas cannot be governed as user schemas")
        config.update(executor="trino", catalog=catalog_name(ds), source_kind=ds.kind)
    if body.prefix is not None:
        if ds.kind != "s3":
            raise HTTPException(422, "prefix is only valid for S3 namespace resources")
        from ..storage_access import prefix_path
        prefix_path(ds.config.get("prefix", ""), body.prefix)
        config["prefix"] = body.prefix
    r = Resource(kind="schema", name=body.name, parent_id=parent.id, object_id=ds.id, config=config)
    db.add(r)
    db.flush()
    rbac.own(db,user,r)
    audit(db, user.username, "schema.register", r.id, source=name, schema=body.name)
    db.commit()
    return resource_out(db, r, user)


@router.get("/resources/{resource_id}")
def read_resource(resource_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """One governed object, for automation that reads back what it declared (import/drift)."""
    user = current(db, user)
    r = get_resource(db, resource_id)
    rbac.require(db, user, "read", r)
    if r.kind in ('kafka', 'topic') and r.config.get('deleted'):
        raise HTTPException(404, 'Kafka object not found')
    return resource_out(db, r, user)


@router.delete("/resources/{resource_id}", status_code=204)
def deregister_schema(resource_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Stop governing a schema or namespace. Its data in the source is never touched."""
    user = current(db, user)
    r = get_resource(db, resource_id)
    if r.kind != "schema":
        raise HTTPException(409, "only schema registrations are removed here; use the object's own API")
    rbac.require(db, user, "delete", r)
    rbac.delete_resource_assignments(db, r.id)
    db.delete(r)
    audit(db, user.username, "schema.deregister", resource_id, schema=r.name)
    db.commit()


@router.get("/resources/{resource_id}/assignments")
def assignments(resource_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    r = get_resource(db, resource_id)
    rbac.require(db, user, "read", r)
    chain = {x.id for x in rbac.ancestors(db, r)}
    groups = [{"id": a.id, "resource_id": a.resource_id, "app_group_id": a.app_group_id,
             "group": db.get(AppGroup, a.app_group_id).name, "role": a.role,
             "principal_id": None, "principal_name": None,
             "inherited": a.resource_id != r.id}
            for a in db.scalars(select(RoleAssignment).where(RoleAssignment.resource_id.in_(chain)))]
    services = [{"id": a.id, "resource_id": a.resource_id, "app_group_id": None, "group": None,
                 "principal_id": a.principal_id, "principal_name": db.get(DirectoryPrincipal, a.principal_id).name,
                 "principal_active": db.get(DirectoryPrincipal, a.principal_id).active,
                 "principal_kind": db.get(DirectoryPrincipal, a.principal_id).kind,
                 "role": a.role, "inherited": a.resource_id != r.id}
                for a in db.scalars(select(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id.in_(chain)))]
    groups = [g for g in groups if g["app_group_id"] != GLOBAL_ADMIN]
    groups.insert(0,{"id":"system-global-admin", "resource_id":r.id, "app_group_id":GLOBAL_ADMIN,
        "group":"Global Admin", "role":"Owner", "principal_id":None,"principal_name":None,
        "inherited":False,"system":True})
    return groups + services


@router.get("/resources/{resource_id}/service-principals")
def service_principals(resource_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    rbac.require(db, user, "assign", get_resource(db, resource_id))
    # Resource Owners need a minimal service picker without access to the entire IdP directory.
    from ..environments import principal_environment, resource_environment
    environment = resource_environment(db, get_resource(db, resource_id))
    return [{"id": p.id, "name": p.name} for p in db.scalars(select(DirectoryPrincipal).where(
        DirectoryPrincipal.kind == "service", DirectoryPrincipal.active.is_(True)).order_by(DirectoryPrincipal.name))
        if principal_environment(db, p.id) == environment]


@router.get("/resources/{resource_id}/user-principals")
def user_principals(resource_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    rbac.require(db, user, "assign", get_resource(db, resource_id))
    return [{"id": p.id, "name": p.name} for p in db.scalars(select(DirectoryPrincipal).where(
        DirectoryPrincipal.kind == "user", DirectoryPrincipal.active.is_(True)).order_by(DirectoryPrincipal.name))]


def require_justification(db, resource, body) -> None:
    """Under an access policy a role is given only with an approved case for exactly this access."""
    from .. import compliance
    from ..models import AccessCase
    existing = db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == resource.id,
                                                      RoleAssignment.app_group_id == body.app_group_id)) if body.app_group_id else None
    roles = list(rbac.roles_for(resource))
    if existing is not None and existing.role in roles and roles.index(existing.role) >= roles.index(body.role):
        return  # not giving more than the group has
    req = compliance.role_requirement(db, resource, body.role, body.app_group_id)
    if req["policy"] is None:
        return
    if body.principal_id:
        raise HTTPException(409, {"code": "justification_required", "message": "access to this data needs a compliance form: give it to a group"})
    case = db.get(AccessCase, body.justification_id) if body.justification_id else None
    valid = (case is not None and case.status == "approved" and case.kind == "role" and case.resource_id == resource.id
             and case.app_group_id == body.app_group_id and case.role in roles and roles.index(case.role) >= roles.index(body.role)
             and (case.approved_until is None or compliance._aware(case.approved_until) > utcnow()))
    if not valid:
        raise HTTPException(409, {"code": "justification_required",
                                  "message": "access to this data needs an approved compliance form",
                                  "requirement": compliance.requirement_out(db, req)})


@router.put("/resources/{resource_id}/assignments")
def assign(resource_id: str, body: AssignmentIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    r = get_resource(db, resource_id)
    rbac.require(db, user, "assign", r)
    if body.role not in rbac.roles_for(r):
        raise HTTPException(422, "choose a valid built-in role for this resource")
    require_justification(db, r, body)
    if body.principal_id:
        principal = db.get(DirectoryPrincipal, body.principal_id)
        if not principal or not principal.active or principal.kind not in ("service", "user"):
            raise HTTPException(422, "choose an active user or service identity")
        from ..environments import principal_environment, resource_environment
        if principal.kind == "service" and principal_environment(db, principal.id) != resource_environment(db, r):
            raise HTTPException(422, "service and resource must belong to the same layer")
        model, recipient = ServiceRoleAssignment, {"principal_id": principal.id}
        condition = ServiceRoleAssignment.principal_id == principal.id
    else:
        if body.app_group_id == GLOBAL_ADMIN:
            raise HTTPException(409,"Global Admin already has protected Owner access to every object")
        if not db.get(AppGroup, body.app_group_id):
            raise HTTPException(422, "choose a valid app group")
        model, recipient = RoleAssignment, {"app_group_id": body.app_group_id}
        condition = RoleAssignment.app_group_id == body.app_group_id
    a = db.scalar(select(model).where(model.resource_id == r.id, condition))
    before = a.role if a else None
    if a is None:
        a = model(resource_id=r.id, **recipient, role=body.role)
        db.add(a)
    a.role = body.role
    audit(db, user.username, "role.assign", r.id, group=body.app_group_id, principal_id=body.principal_id,
          before=before, role=body.role)
    db.commit()
    return assignments(resource_id, db, user)


@router.delete("/resources/{resource_id}/assignments/{app_group_id}", status_code=204)
def revoke(resource_id: str, app_group_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    r = get_resource(db, resource_id)
    rbac.require(db, user, "assign", r)
    if app_group_id == GLOBAL_ADMIN:
        raise HTTPException(409,"Global Admin access cannot be revoked on an object")
    db.execute(delete(RoleAssignment).where(RoleAssignment.resource_id == r.id,
                                          RoleAssignment.app_group_id == app_group_id))
    audit(db, user.username, "role.revoke", r.id, group=app_group_id)
    db.commit()


@router.delete("/resources/{resource_id}/assignments/services/{principal_id}", status_code=204)
def revoke_service(resource_id: str, principal_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    r = get_resource(db, resource_id)
    rbac.require(db, user, "assign", r)
    db.execute(delete(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id == r.id,
                                                  ServiceRoleAssignment.principal_id == principal_id))
    audit(db, user.username, "role.revoke", r.id, principal_id=principal_id)
    db.commit()


@router.get("/resources/{resource_id}/access")
def effective_access(resource_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    r = get_resource(db, resource_id)
    rbac.require(db, user, "read", r)
    return {"resource": resource_out(db, r, user), "grants": rbac.access(db, user, r)}


@router.post("/resources/{resource_id}/provision")
def provision(resource_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    from ..data_access import provision_schema
    r = get_resource(db, resource_id)
    if r.kind != "schema":
        raise HTTPException(422, "only schema provisioning is supported")
    rbac.require(db, user, "assign", r)
    ds = db.get(DataSource, r.object_id)
    try:
        provision_schema(ds, r, get_secret_store())
    except HTTPException:
        raise
    except Exception:
        import logging
        logging.getLogger(__name__).exception("native provisioning failed for resource %s", r.id)
        r.config = {**r.config, "state": "failed"}
        audit(db, user.username, "schema.provision_failed", r.id)
        db.commit()
        raise HTTPException(409, "native provisioning failed; check provisioning privileges and database compatibility") from None
    audit(db, user.username, "schema.provision", r.id, source=ds.name)
    db.commit()
    return resource_out(db, r, user)
