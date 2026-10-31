"""Application-owned RBAC. Additive inheritance, fixed roles, live directory bindings."""
from __future__ import annotations

from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from .auth import User
from .authorization_cache import cached
from .config import get_settings
from .models import (AppGroup, DirectoryMembership, DirectoryPrincipal, IdentityBinding,
                     Resource, RoleAssignment, ServiceRoleAssignment, ServiceAccount)

PROGRAM_ROLES = {
    "Executor": {"read", "execute"},
    "Contributor": {"read", "execute", "edit", "deploy"},
    "Admin": {"read", "execute", "edit", "deploy", "create", "delete"},
    "Owner": {"read", "execute", "edit", "deploy", "create", "delete", "assign", "identity"},
}
# API management: Consumer may call (external APIs, as a program or person); management is separate.
API_ROLES = {
    "Consumer": {"read", "call"},
    "Contributor": {"read", "call", "edit"},
    "Admin": {"read", "call", "edit", "create", "delete"},
    "Owner": {"read", "call", "edit", "create", "delete", "assign", "identity"},
}
API_KINDS = ("api-root", "api", "api-group", "api-operation")
# The package repository: everyone reads. On the repository, Publisher publishes new packages and imports
# from PyPI/CRAN; a package's Owner (the publishing group) and Maintainers publish its new versions.
PACKAGE_ROLES = {
    "Publisher": {"read", "create", "import"},
    "Maintainer": {"read", "create", "import", "publish", "yank"},
    "Owner": {"read", "create", "import", "publish", "yank", "delete", "assign"},
}
PACKAGE_KINDS = ("package-root", "package")
# Data products: Consumer may read or use everything in the product (see product_grants); Owner also
# manages the product and decides access requests.
PRODUCT_ROLES = {
    "Consumer": {"read", "use"},
    "Owner": {"read", "use", "assign"},
}
PRODUCT_KINDS = ("product-root", "product")
# What a product's consumers get on each object in it: the role that lets someone read or use it.
READER_ROLE = {"schema": "Reader", "source": "Reader", "topic": "Reader", "kafka": "Reader", "service": "Executor",
               "api": "Consumer", "api-group": "Consumer", "api-operation": "Consumer"}
DATA_ROLES = {
    "Reader": {"read"},
    "Contributor": {"read", "write"},
    "Admin": {"read", "write", "ddl"},
    "Owner": {"read", "write", "ddl", "assign"},
}


def principal(db: Session, kind: str, external_id: str, name: str = "", provider: Optional[str] = None):
    provider = provider or get_settings().oidc_issuer
    p = db.scalar(select(DirectoryPrincipal).where(DirectoryPrincipal.kind == kind,
                  DirectoryPrincipal.provider == provider, DirectoryPrincipal.external_id == external_id))
    if p is None:
        from .models import new_id
        dialect = db.bind.dialect.name
        if dialect in ("postgresql", "sqlite"):
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            db.execute(insert(DirectoryPrincipal).values(id=new_id(), kind=kind, external_id=external_id,
                name=name or external_id, provider=provider, active=True, managed=False, attributes={})
                .on_conflict_do_nothing(index_elements=["provider", "kind", "external_id"]))
            p = db.scalar(select(DirectoryPrincipal).where(DirectoryPrincipal.kind == kind,
                          DirectoryPrincipal.provider == provider, DirectoryPrincipal.external_id == external_id))
        else:
            p = DirectoryPrincipal(kind=kind, external_id=external_id, name=name or external_id, provider=provider)
            db.add(p)
            db.flush()
    return p


@cached
def resolve_user(db: Session, user: User, refresh: bool = False) -> User:
    from dataclasses import replace
    from .environments import principal_environment
    from .layers import canonical as canonical_layer
    resolved = _resolve_user(db, user, refresh)
    environment = canonical_layer(db, user.environment_id) if user.environment_id is not None else None
    if resolved.username.startswith(("service:", "sa:")):
        canonical = principal_environment(db, resolved.principal_id)
        if environment is not None and canonical != environment:
            raise HTTPException(403, "execution identity belongs to another layer")
        environment = canonical
    resolved = replace(resolved, environment_id=environment, execution_context=user.execution_context,
                       roles=[] if user.execution_context else resolved.roles,
                       global_admin=False if user.execution_context else resolved.global_admin)
    from .ownership import selected_group_id
    return replace(resolved, active_group_id=user.active_group_id if user.active_group_id is not None else selected_group_id(db, resolved))


def _resolve_user(db: Session, user: User, refresh: bool = False) -> User:
    """SCIM is authoritative once an identity is provisioned. OIDC-only dev identities are persisted."""
    if user.username.startswith("service:"):
        p = db.get(DirectoryPrincipal, user.username[8:])
        if not p or p.kind != "service" or not p.active:
            raise HTTPException(401, "service disabled or missing")
        return User(username=user.username, principal_id=p.id)
    if user.username.startswith("sa:"):
        sa = db.scalar(select(ServiceAccount).where(ServiceAccount.name == user.username[3:]))
        if not sa or sa.disabled:
            raise HTTPException(401, "service account disabled or missing")
        p = principal(db, "service", sa.id, sa.name, "local")
        return User(username=user.username, groups=list(sa.groups), principal_id=p.id)
    p = db.get(DirectoryPrincipal, user.principal_id) if user.principal_id else None
    if p is None:
        provider = user.issuer or get_settings().oidc_issuer
        # Explicit subject matching is preferred. SCIM userName is a bootstrap mapping only
        # within the configured issuer; once bound, the subject cannot be rebound by a rename.
        candidates = list(db.scalars(select(DirectoryPrincipal).where(
            DirectoryPrincipal.kind == "user", DirectoryPrincipal.provider == provider)))
        p = next((x for x in candidates if user.subject and x.subject == user.subject), None)
        if p is None:
            p = next((x for x in candidates if x.name.casefold() == user.username.casefold() and
                      (not user.subject or not x.subject)), None)
        if p is None:
            p = principal(db, "user", user.subject or user.username, user.username, provider)
    # Serialize claim refreshes for the same user. The desktop authenticates several
    # requests concurrently; delete/reinsert without this lock can race membership writes.
    query = select(DirectoryPrincipal).where(DirectoryPrincipal.id == p.id)
    # Read-only authorization must not serialize every dashboard/poll/SQL request for
    # the same SCIM user. Only actual claim refresh or first subject binding writes.
    if (refresh and not p.managed) or (user.subject and not p.subject):
        query = query.with_for_update()
    p = db.scalar(query.execution_options(populate_existing=True))
    if not p.active:
        raise HTTPException(401, "identity is inactive")
    if get_settings().directory_mode == "scim" and not p.managed:
        raise HTTPException(401, "identity must be provisioned through SCIM before signing in")
    if user.subject:
        if p.subject and p.subject != user.subject:
            raise HTTPException(401, "OIDC subject does not match the provisioned identity")
        if not p.subject:
            p.subject = user.subject
    if refresh and not p.managed and user.email and (p.attributes or {}).get("email") != user.email:
        p.attributes = {**(p.attributes or {}), "email": user.email}  # where notifications go
    if refresh and not p.managed:
        # OIDC-only users can operate before a production SCIM provider is chosen.
        # Never replace SCIM-managed group memberships with stale JWT claims.
        memberships = list(db.scalars(select(DirectoryMembership).where(DirectoryMembership.member_id == p.id)))
        desired = set()
        for kind, names in (("group", user.groups), ("role", user.roles)):
            for name in names:
                group = principal(db, kind, name, name, p.provider)
                if not group.managed:
                    desired.add(group.id)
        existing = {m.group_id for m in memberships}
        for membership in memberships:
            group = db.get(DirectoryPrincipal, membership.group_id)
            if group and not group.managed and group.id not in desired:
                db.delete(membership)
        for ident in desired - existing:
            db.add(DirectoryMembership(group_id=ident, member_id=p.id))
        db.flush()

    ids = closure(db, p.id)
    facts = [db.get(DirectoryPrincipal, x) for x in ids if x != p.id]
    from .ownership import GLOBAL_ADMIN
    global_admin = bool(db.scalar(select(IdentityBinding.id).where(
        IdentityBinding.app_group_id == GLOBAL_ADMIN, IdentityBinding.principal_id.in_(ids))))
    return User(username=p.name, groups=[x.name for x in facts if x and x.kind == "group"],
                roles=[x.name for x in facts if x and x.kind == "role"],
                admin_role=get_settings().admin_role, principal_id=p.id, subject=p.subject or "", issuer=p.provider,
                global_admin=global_admin)


@cached
def closure(db: Session, member_id: str) -> set:
    result, pending = {member_id}, [member_id]
    while pending:
        for group_id in db.scalars(select(DirectoryMembership.group_id).where(
                DirectoryMembership.member_id == pending.pop())):
            g = db.get(DirectoryPrincipal, group_id)
            if g and g.active and group_id not in result:
                result.add(group_id)
                pending.append(group_id)
    return result


@cached
def app_groups(db: Session, user: User) -> set:
    user = resolve_user(db, user)
    ids = closure(db, user.principal_id)
    return set(db.scalars(select(IdentityBinding.app_group_id).where(IdentityBinding.principal_id.in_(ids))))


def ancestors(db: Session, resource: Resource) -> list:
    chain, seen = [], set()
    while resource:
        if resource.id in seen:
            raise HTTPException(409, "resource cycle detected")
        seen.add(resource.id)
        chain.append(resource)
        resource = db.get(Resource, resource.parent_id) if resource.parent_id else None
    return chain


def roles_for(resource: Resource) -> dict:
    if resource.kind in API_KINDS:
        return API_ROLES
    if resource.kind in PACKAGE_KINDS:
        return PACKAGE_ROLES
    if resource.kind in PRODUCT_KINDS:
        return PRODUCT_ROLES
    return DATA_ROLES if resource.kind in ("source", "schema", "kafka", "topic") else PROGRAM_ROLES


def role_grants(db: Session, user: User, resource_ids=None) -> list:
    """One authorization path for group and direct-service grants, including bulk callers."""
    user = resolve_user(db, user)
    result = _role_grants(db, user)
    if resource_ids is not None:
        return [g for g in result if g.resource_id in resource_ids]
    from .environments import permits
    return [g for g in result if permits(db, user, db.get(Resource, g.resource_id))]


class ProductGrant:
    """A role a group holds on an object because it holds Consumer (or Owner) on a data product containing it.
    Never stored: revoking the product role, removing the object from the product or unpublishing ends it."""

    def __init__(self, resource_id, role, app_group_id, product_resource_id):
        self.resource_id, self.role, self.app_group_id = resource_id, role, app_group_id
        self.product_resource_id = product_resource_id


def product_grants(db: Session, assignments) -> list:
    """Consumers and owners of a data product may read or use everything in it (under an access policy: what their
    approval covered; objects added later wait for a new approval)."""
    from .models import CatalogAsset, CatalogProductMember, ProductAccess
    out = []
    for a in assignments:
        if a.role not in PRODUCT_ROLES:
            continue
        product = db.get(Resource, a.resource_id)
        if not product or product.kind != "product":
            continue
        access = db.scalar(select(ProductAccess).where(ProductAccess.product_id == product.object_id,
                                                       ProductAccess.app_group_id == a.app_group_id)) if a.role == "Consumer" else None
        covered = set(access.approved_assets) if access is not None and access.approved_assets is not None else None
        for m in db.scalars(select(CatalogProductMember).where(CatalogProductMember.product_id == product.object_id)):
            if covered is not None and m.asset_id not in covered:
                continue
            asset = db.get(CatalogAsset, m.asset_id)
            target = db.get(Resource, asset.resource_id) if asset else None
            if target is not None and target.kind in READER_ROLE:
                out.append(ProductGrant(target.id, READER_ROLE[target.kind], a.app_group_id, product.id))
    return out


def group_grants(db: Session, group_ids, user=None) -> list:
    """Role assignments of these groups, plus what their data product roles give them. With a user: justified
    access of a group counts only once the user's membership of it is approved."""
    assigned = list(db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id.in_(list(group_ids)))))
    if user is not None:
        from .compliance import unapproved_justified
        held = unapproved_justified(db, user, group_ids)
        if held:
            assigned = [a for a in assigned if (a.resource_id, a.app_group_id) not in held]
    return assigned + product_grants(db, assigned)


@cached
def _role_grants(db, user):
    groups = app_groups(db, user)
    result = group_grants(db, groups, user)
    principal = db.get(DirectoryPrincipal, user.principal_id)
    if principal and principal.active and principal.kind in ("service", "user"):
        result.extend(db.scalars(select(ServiceRoleAssignment).where(ServiceRoleAssignment.principal_id == user.principal_id)))
    return result


def delete_resource_assignments(db: Session, resource_id: str):
    for model in (RoleAssignment, ServiceRoleAssignment):
        db.execute(delete(model).where(model.resource_id == resource_id))


def has_resource_assignments(db: Session, resource_id: str) -> bool:
    return any(db.scalar(select(model.id).where(model.resource_id == resource_id).limit(1))
               for model in (RoleAssignment, ServiceRoleAssignment))


@cached
def _grant_group(db, ident):
    return db.get(AppGroup, ident)


@cached
def _grant_principal(db, ident):
    return db.get(DirectoryPrincipal, ident)


@cached
def access(db: Session, user: User, resource: Resource) -> list:
    from .environments import permits
    user = resolve_user(db, user)
    if not permits(db, user, resource):
        return []
    chain = {r.id: r for r in ancestors(db, resource)}
    result = []
    if user.is_admin and not user.execution_context:
        from .ownership import GLOBAL_ADMIN
        result.append({"role":"Owner", "group_id":GLOBAL_ADMIN, "group":"Global Admin",
                       "principal_id":None, "principal_name":None, "resource_id":resource.id,
                       "resource":resource.name, "inherited":False, "system":True})
    for grant in role_grants(db, user, chain):
        direct = isinstance(grant, ServiceRoleAssignment)
        group = _grant_group(db, grant.app_group_id) if not direct else None
        principal = _grant_principal(db, grant.principal_id) if direct else None
        result.append({"role": grant.role, "group_id": group.id if group else None, "group": group.name if group else None,
                       "principal_id": principal.id if principal else None,
                       "principal_name": principal.name if principal else None,
                       "resource_id": grant.resource_id, "resource": chain[grant.resource_id].name,
                       "inherited": grant.resource_id != resource.id})
    return result


def allowed(db: Session, user: User, action: str, resource: Resource) -> bool:
    if not resource:
        return False
    from .environments import permits
    user = resolve_user(db, user)
    from .platform_metadata import is_resource, authorized
    if is_resource(resource):
        return authorized(user) and action == "read"
    if resource.kind in PACKAGE_KINDS and action == "read":
        return True  # every identity may install packages and see who publishes them
    if not permits(db, user, resource):
        return False
    # Interactive Global Admin has protected access; layer checks precede every grant.
    if user.is_admin and action in {"assign", "identity", "create", "delete", "edit", "deploy", "read", "execute",
                                    "import", "publish", "yank"}:
        return True
    definitions = roles_for(resource)
    return any(action in definitions.get(a["role"], set()) for a in access(db, user, resource))


def require(db: Session, user: User, action: str, resource: Resource):
    if not allowed(db, user, action, resource):
        raise HTTPException(403, f"{action} permission required")


def owner_group(db: Session, user: User) -> AppGroup:
    from .ownership import owner_group as selected_owner
    return selected_owner(db, user)


def own(db: Session, user: User, resource: Resource):
    group = owner_group(db, user)
    resource.config = {**(resource.config or {}), "owner_group_id":group.id}
    grant = db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == resource.id,
                                                  RoleAssignment.app_group_id == group.id))
    if grant:
        grant.role = "Owner"
    else:
        db.add(RoleAssignment(resource_id=resource.id, app_group_id=group.id, role="Owner"))
    db.flush()


def _fixed_resource(db: Session, ident: str, kind: str, name: str) -> Resource:
    """A well-known root, created once even when concurrent requests both find it missing."""
    item = db.get(Resource, ident)
    if item is None:
        from sqlalchemy.exc import IntegrityError
        try:
            with db.begin_nested():
                db.add(Resource(id=ident, kind=kind, name=name))
        except IntegrityError:
            pass  # another request created it first
        item = db.get(Resource, ident)
    return item


def products_root(db: Session) -> Resource:
    """Parent of every data product resource."""
    return _fixed_resource(db, "products", "product-root", "Data products")


def api_root(db: Session) -> Resource:
    """Parent of every API; grants here cover all APIs, groups and operations."""
    return _fixed_resource(db, "apis", "api-root", "APIs")


def packages_root(db: Session) -> Resource:
    """The package repository; grants here cover every internal package in it."""
    return _fixed_resource(db, "packages", "package-root", "Packages")


def root(db: Session) -> Resource:
    item = _fixed_resource(db, "workloads", "root", "Workloads")
    if item.name == "Deployed":
        item.name = "Workloads"
    return item


def source_resource(db: Session, source) -> Resource:
    from .layers import layer_of
    r = db.scalar(select(Resource).where(Resource.kind == "source", Resource.object_id == source.id))
    if r is None:
        r = Resource(kind="source", name=source.name, object_id=source.id, config={"source_kind": source.kind, "environment_id": source.config.get("environment_id", ""), "layer_id": layer_of(db, source.config.get("layer_id") or source.config.get("environment_id"))})
        db.add(r)
        db.flush()
    return r


def data_scope(db: Session, user: User, source) -> dict:
    """Schema -> effective level, including protected interactive Global Admin access."""
    result = {}
    r = source_resource(db, source)
    for schema in db.scalars(select(Resource).where(Resource.kind == "schema", Resource.parent_id == r.id)):
        grants = access(db, user, schema)
        ranks = [a["role"] for a in grants if a["role"] in DATA_ROLES]
        if ranks:
            result[schema.name] = max(ranks, key=lambda role: list(DATA_ROLES).index(role))
    return result


def workload_resource(db: Session, pub) -> Resource:
    if not pub.service_principal_id:
        p = principal(db, "service", pub.id, pub.title, "local")
        pub.service_principal_id = p.id
        db.flush()
    r = db.get(Resource, pub.resource_id) if pub.resource_id else None
    if r is None:
        # Explicit migration of old workloads: preserve deployer management, but do not
        # silently convert its credentials or everyone visibility into new broad grants.
        r = Resource(kind="service", name=pub.title, parent_id=root(db).id, object_id=pub.id)
        db.add(r)
        db.flush()
        pub.resource_id = r.id
        pub.owner_principal_id = own_legacy(db, pub.owner, r)
        db.flush()
    return r


def service_account_resource(db: Session, sa) -> Resource:
    item = db.scalar(select(Resource).where(Resource.kind == "service", Resource.object_id == sa.id))
    p = principal(db, "service", sa.id, sa.name, "local")
    p.active = not sa.disabled
    if item is None:
        item = Resource(kind="service", name=f"Service account: {sa.name}", object_id=sa.id,
                        config={"service_account": True, "principal_id": p.id, "account_name": sa.name})
        db.add(item)
        db.flush()
    return item


def own_legacy(db: Session, username: str, resource: Resource):
    """Migrate trusted persisted ownership without authenticating an unprovisioned user."""
    if username.startswith("sa:"):
        sa = db.scalar(select(ServiceAccount).where(ServiceAccount.name == username[3:]))
        p = principal(db, "service", sa.id if sa else username, username, "local")
    else:
        p = principal(db, "user", username, username)
    db.add(ServiceRoleAssignment(resource_id=resource.id, principal_id=p.id, role="Owner"))
    db.flush()
    return p.id


def same_identity(item, user):
    """Names are only a compatibility fallback for records without a stable identity."""
    return item.principal_id == user.principal_id if item.principal_id else item.owner == user.username
