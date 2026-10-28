"""Global execution boundaries. A matching layer narrows RBAC; it never grants access."""
from fastapi import HTTPException
from .authorization_cache import cached
from sqlalchemy import delete, select, text

from .models import (ApiDefinition, AppGroup, AuditEvent, DirectoryPrincipal, IdentityBinding, PlatformSetting,
                     Resource, RoleAssignment, Run, ServiceRoleAssignment, DataSource, Workspace)

# Layers come only from configuration (OpenTofu or an administrator). These are the IDs of the Prod and
# Non-Prod layers that installations before 2026-10 created by themselves; they stay ordinary layers there.
PROD = "1a0000000001"
NON_PROD = "1a0000000002"
DEFAULTS = "layers"  # platform setting: {"default": layer id}


def all_layers(db):
    return list(db.scalars(select(Resource).where(Resource.kind == "layer").order_by(Resource.name)))


@cached
def default_layer(db):
    """Where work without a layer goes: the configured default, or the only layer there is; else None."""
    setting = db.get(PlatformSetting, DEFAULTS)
    ident = (setting.value or {}).get("default") if setting else None
    layer = db.get(Resource, ident) if ident else None
    if layer is not None and layer.kind == "layer":
        return layer.id
    only = db.scalars(select(Resource.id).where(Resource.kind == "layer").limit(2)).all()
    return only[0] if len(only) == 1 else None


def require_default(db):
    ident = default_layer(db)
    if ident:
        return ident
    if db.scalar(select(Resource.id).where(Resource.kind == "layer").limit(1)):
        raise HTTPException(422, "choose a layer (no default layer is configured)")
    raise HTTPException(422, "no layers are configured yet; an administrator creates them (OpenTofu fdp_layer or the Layers page)")


def is_production(db, ident):
    layer = db.get(Resource, ident) if ident else None
    return bool(layer is not None and (layer.config or {}).get("production"))


def legacy_layer(db, production):
    """For old environment folders: the one layer with that production setting, else the default."""
    matches = [layer.id for layer in all_layers(db) if bool((layer.config or {}).get("production")) == production]
    return matches[0] if len(matches) == 1 else require_default(db)


def layer_of(db, ident):
    """canonical() for reading: an unbound object is in the default layer, or in none (None) without one."""
    if not ident:
        return default_layer(db)
    try:
        return canonical(db, ident)
    except HTTPException:
        return None


@cached
def canonical(db, ident):
    if not ident:
        return require_default(db)
    resource = db.get(Resource, ident)
    if resource and resource.kind == "layer":
        return resource.id
    if resource and resource.config.get("layer_id"):
        return resource.config["layer_id"]
    # Compatibility for old folder-bound API clients; new UI sends layer_id.
    if resource and resource.config.get("environment"):
        production = resource.name.casefold() in ("prod", "prd", "production") or resource.config.get("branch") in ("main", "master", "prod")
        return legacy_layer(db, production)
    raise HTTPException(422, "choose a valid layer")


def require_layer(db, ident=None):
    layer = db.get(Resource, canonical(db, ident))
    if not layer or layer.kind != "layer":
        raise HTTPException(422, "choose a valid layer")
    return layer


def program_layer(db, program):
    """Portable names or stable layer IDs in fdp.json, without folder aliases."""
    if program.layer is None:
        return None
    candidates = all_layers(db)
    layer = next((item for item in candidates if item.id == program.layer), None)
    layer = layer or next((item for item in candidates if item.name.casefold() == program.layer.casefold()), None)
    if layer is None:
        raise HTTPException(422, f"fdp.json references an unknown layer: {program.layer!r}")
    return layer


def require_program_layer(db, program, target, status_code=422):
    declared = program_layer(db, program)
    actual = require_layer(db, target)
    if declared and declared.id != actual.id:
        raise HTTPException(status_code, f"program requires layer {declared.name!r}; this execution is bound to {actual.name!r}")


def explicit_layer(db, resource):
    from .rbac import ancestors
    for item in ancestors(db, resource) if resource else []:
        if item.kind == "layer":
            return item.id
        config = item.config or {}
        if config.get("layer_id") or config.get("environment_id") or config.get("environment"):
            return canonical(db, config.get("layer_id") or config.get("environment_id") or item.id)
    return ""


@cached
def resource_layer(db, resource):
    from .platform_metadata import is_resource
    if is_resource(resource):
        return ""  # Platform metadata spans layers and is never an execution data source.
    layer = explicit_layer(db, resource)
    if layer:
        return layer
    # Organization folders may span layers; every actual executable/data/API object is bound.
    return (default_layer(db) or "") if resource and resource.kind not in ("root", "folder", "api-root") else ""


def info(db, resource):
    ident = resource_layer(db, resource)
    layer = db.get(Resource, ident) if ident else None
    return {"layer_id": ident or None, "layer_name": layer.name if layer else None}


def selection(db, ident=None, parent=None):
    inherited = explicit_layer(db, parent)
    selected = require_layer(db, ident or inherited)
    if inherited and inherited != selected.id:
        raise HTTPException(422, "an object must use its parent folder's layer")
    return selected.id


def _legacy_layers(db):
    """Data from before layers existed needs the two layers it was implicitly in."""
    for ident, name, production in ((PROD, "Prod", True), (NON_PROD, "Non-Prod", False)):
        if not db.get(Resource, ident):
            db.add(Resource(id=ident, kind="layer", name=name,
                            config={"workspaces": True, "description": "", "production": production}))
    if not db.get(PlatformSetting, DEFAULTS):
        db.add(PlatformSetting(key=DEFAULTS, value={"default": NON_PROD}))
    db.flush()


def adopt_initial_layers(db):
    """Installations that got Prod and Non-Prod built in keep them as ordinary, configurable layers:
    Prod is a production layer and Non-Prod the default, as they behaved before."""
    if db.execute(text("SELECT revision FROM fdp_migrations WHERE revision='003_configured_layers'")).first():
        return
    for layer in all_layers(db):
        config = dict(layer.config or {})
        if config.pop("initial_layer", None):
            config.setdefault("production", layer.id == PROD)
            layer.config = config
            if layer.id == NON_PROD and not db.get(PlatformSetting, DEFAULTS):
                db.add(PlatformSetting(key=DEFAULTS, value={"default": NON_PROD}))
    db.execute(text("INSERT INTO fdp_migrations(revision) VALUES ('003_configured_layers')"))
    db.flush()


def migrate(db):
    """Preserve Test/Prod rights and private volumes while decoupling folder membership."""
    adopt_initial_layers(db)
    if db.execute(text("SELECT revision FROM fdp_migrations WHERE revision='002_layers'")).first():
        return
    if any(db.scalar(select(model.id).limit(1)) for model in (Workspace, Run, DataSource, ApiDefinition)) or \
            db.scalar(select(Resource.id).where(Resource.kind == "folder").limit(1)):
        _legacy_layers(db)
    from .rbac import roles_for
    for resource in db.scalars(select(Resource).where(Resource.kind == "folder")):
        if resource.config.get("environment") and not resource.config.get("layer_id"):
            resource.config = {**resource.config, "layer_id": canonical(db, resource.id)}
    for model in (DataSource, ApiDefinition):
        for item in db.scalars(select(model)):
            config = item.config or {}
            item.config = {**config, "layer_id": canonical(db, config.get("layer_id") or config.get("environment_id"))}
    for resource in db.scalars(select(Resource).where(Resource.kind.in_(("source", "service", "api")))):
        resource.config = {**resource.config, "layer_id": resource_layer(db, resource)}
    for model in (Workspace, Run):
        for item in db.scalars(select(model)):
            item.environment_id = canonical(db, item.environment_id)
    # This fixture group existed only to give Ana her own dbt development schema.
    # Preserve the exact users/roles as direct grants; never broaden them to all developers.
    retired = 0
    group = db.scalar(select(AppGroup).where(AppGroup.name == "Pokemon Test Workspace Ana"))
    if group:
        bindings = list(db.scalars(select(IdentityBinding).where(IdentityBinding.app_group_id == group.id)))
        principals = [db.get(DirectoryPrincipal, b.principal_id) for b in bindings]
        if principals and all(p and p.kind == "user" for p in principals):
            for grant in db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == group.id)):
                for principal in principals:
                    direct = db.scalar(select(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id == grant.resource_id,
                        ServiceRoleAssignment.principal_id == principal.id))
                    if not direct:
                        db.add(ServiceRoleAssignment(resource_id=grant.resource_id, principal_id=principal.id, role=grant.role))
                    elif set(roles_for(db.get(Resource, grant.resource_id))[direct.role]) < set(roles_for(db.get(Resource, grant.resource_id))[grant.role]):
                        direct.role = grant.role
            db.execute(delete(RoleAssignment).where(RoleAssignment.app_group_id == group.id))
            db.execute(delete(IdentityBinding).where(IdentityBinding.app_group_id == group.id))
            db.delete(group)
            retired = 1
    db.add(AuditEvent(actor="system", action="migration.layers", target="002_layers", detail={"workspace_groups_retired": retired}))
    db.execute(text("INSERT INTO fdp_migrations(revision) VALUES ('002_layers')"))
    db.flush()
