from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import layers, rbac
from ..auth import User
from ..db import get_db
from ..deps import get_actor
from ..models import ApiDefinition, DataSource, Resource, Run, Workspace
from .. import layer_infrastructure as infrastructure
from ..layer_infrastructure import InfrastructureChange, InfrastructureRetry, InfrastructureSpec
from .common import audit
from .. import settings_access

router = APIRouter(tags=["layers"])


@router.get('/v1/admin/layers/{ident}/infrastructure')
def infrastructure_state(ident: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    settings_access.require(db, user, "set-layers", "view")
    layer = layers.require_layer(db, ident)
    from .. import cert_monitor
    return {**infrastructure.state(db, layer), 'certificates': [cert_monitor.out(r) for r in cert_monitor.rows(db, layer.id)]}


@router.get('/v1/admin/certificates')
def certificates(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Every monitored certificate (gateways and layer endpoints), most severe first."""
    settings_access.require(db, user, "set-layers", "view")
    from .. import cert_monitor
    found = [cert_monitor.out(r) for r in cert_monitor.rows(db)]
    return sorted(found, key=lambda c: (-c['severity'], c['not_after'] is None, c['not_after'] or 0, c['key']))


@router.put('/v1/admin/layers/{ident}/infrastructure', status_code=202)
def configure_infrastructure(ident: str, body: InfrastructureChange, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = settings_access.require(db, user, "set-layers")
    if body.spec.retired:
        # Retiring through a full update (e.g. infrastructure-as-code) has the same precondition.
        layer = layers.require_layer(db, ident)
        current = InfrastructureSpec.model_validate(layer.config.get('infrastructure', {}))
        blockers = [] if current.retired else infrastructure.retirement_blockers(db, layer.id)
        if blockers:
            raise HTTPException(409, 'stop this work first: ' + '; '.join(blockers))
    result = infrastructure.configure(db, ident, body)
    audit(db, user.username, 'layer.infrastructure.configure', ident, generation=result['generation'])
    db.commit()
    return result


@router.post('/v1/admin/layers/{ident}/infrastructure/retry', status_code=202)
def retry_infrastructure(ident: str, body: InfrastructureRetry, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = settings_access.require(db, user, "set-layers")
    layer = layers.require_layer(db, ident)
    layer = db.scalar(select(Resource).where(Resource.id == layer.id).with_for_update()
                      .execution_options(populate_existing=True))
    change = InfrastructureChange(expected_generation=body.expected_generation,
        spec=InfrastructureSpec.model_validate(layer.config.get('infrastructure', {})))
    result = infrastructure.configure(db, ident, change, retry=True)
    audit(db, user.username, 'layer.infrastructure.retry', ident, generation=result['generation'])
    db.commit()
    return result


class InfrastructureRetire(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_generation: int = Field(ge=1)


@router.post('/v1/admin/layers/{ident}/infrastructure/retire', status_code=202)
def retire_infrastructure(ident: str, body: InfrastructureRetire, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Scale a managed layer's compute to zero; its namespace, volumes and policies are retained."""
    user = settings_access.require(db, user, "set-layers")
    layer = layers.require_layer(db, ident)
    layer = db.scalar(select(Resource).where(Resource.id == layer.id).with_for_update()
                      .execution_options(populate_existing=True))
    spec = InfrastructureSpec.model_validate(layer.config.get('infrastructure', {}))
    if not spec.enabled:
        raise HTTPException(409, 'this layer has no managed infrastructure to retire')
    blockers = infrastructure.retirement_blockers(db, layer.id)
    if blockers:
        raise HTTPException(409, 'stop this work first: ' + '; '.join(blockers))
    change = InfrastructureChange(expected_generation=body.expected_generation,
                                  spec=spec.model_copy(update={'retired': True}))
    result = infrastructure.configure(db, ident, change)
    audit(db, user.username, 'layer.infrastructure.retire', ident, generation=result['generation'])
    db.commit()
    return result


@router.get('/v1/admin/layers/{ident}/operations')
def infrastructure_operations(ident: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    from ..models import LayerOperation
    settings_access.require(db, user, "set-layers", "view")
    layers.require_layer(db, ident)
    return [infrastructure.operation_out(row) for row in db.scalars(select(LayerOperation).where(
        LayerOperation.layer_id == ident).order_by(LayerOperation.generation.desc()).limit(100))]


class LayerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)
    workspaces: bool = True
    # Personal and group secrets are not usable here; work uses service account secrets.
    production: bool = False


class DefaultLayerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer_id: str = Field(min_length=1, max_length=12)


def out(layer, default=None):
    return {"id": layer.id, "name": layer.name, "description": layer.config.get("description", ""),
            "workspaces": bool(layer.config.get("workspaces")), "production": bool(layer.config.get("production")),
            "default": layer.id == default}


@router.get("/v1/layers")
def listing(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    default = layers.default_layer(db)
    return [out(layer, default) for layer in layers.all_layers(db)
            if user.environment_id is None or layers.canonical(db, user.environment_id) == layer.id]


@router.get("/v1/admin/settings/default-layer")
def default_layer(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """The layer work without one goes to. Unset with exactly one layer: that layer."""
    settings_access.require(db, user, "set-layers", "view")
    from ..models import PlatformSetting
    setting = db.get(PlatformSetting, layers.DEFAULTS)
    return {"layer_id": layers.default_layer(db), "configured": bool(setting and (setting.value or {}).get("default"))}


@router.put("/v1/admin/settings/default-layer")
def set_default_layer(body: DefaultLayerIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = settings_access.require(db, user, "set-layers")
    from ..models import PlatformSetting
    layer = db.get(Resource, body.layer_id)
    if layer is None or layer.kind != "layer":
        raise HTTPException(422, "choose an existing layer")
    row = db.get(PlatformSetting, layers.DEFAULTS) or PlatformSetting(key=layers.DEFAULTS)
    row.value = {"default": layer.id}
    db.add(row)
    audit(db, user.username, "layer.default", layer.id, name=layer.name)
    db.commit()
    return default_layer(db, user)


@router.delete("/v1/admin/settings/default-layer", status_code=204)
def clear_default_layer(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = settings_access.require(db, user, "set-layers")
    from ..models import PlatformSetting
    row = db.get(PlatformSetting, layers.DEFAULTS)
    if row:
        db.delete(row)
    audit(db, user.username, "layer.default.clear", layers.DEFAULTS)
    db.commit()


@router.post("/v1/admin/layers", status_code=201)
def create(body: LayerIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = settings_access.require(db, user, "set-layers")
    from ..ownership import require_create
    require_create(db,user)
    if not body.name.strip():
        raise HTTPException(422, "enter a layer name")
    if any(r.name.casefold() == body.name.strip().casefold() for r in layers.all_layers(db)):
        raise HTTPException(409, "layer name already exists")
    layer = Resource(kind="layer", name=body.name.strip(), config={"description": body.description, "workspaces": body.workspaces,
                                                                   "production": body.production})
    db.add(layer); db.flush()
    rbac.own(db,user,layer)
    audit(db, user.username, "layer.create", layer.id, name=layer.name, workspaces=body.workspaces, production=body.production)
    db.commit()
    return out(layer, layers.default_layer(db))


@router.put("/v1/admin/layers/{ident}")
def update(ident: str, body: LayerIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = settings_access.require(db, user, "set-layers")
    layer = layers.require_layer(db, ident)
    layer = db.scalar(select(Resource).where(Resource.id == layer.id).with_for_update()
                      .execution_options(populate_existing=True))
    if not body.name.strip():
        raise HTTPException(422, "enter a layer name")
    if any(r.id != layer.id and r.name.casefold() == body.name.strip().casefold() for r in layers.all_layers(db)):
        raise HTTPException(409, "layer name already exists")
    layer.name = body.name.strip()
    layer.config = {**layer.config, "description": body.description, "workspaces": body.workspaces, "production": body.production}
    audit(db, user.username, "layer.update", layer.id, name=layer.name, workspaces=body.workspaces, production=body.production)
    db.commit()
    return out(layer, layers.default_layer(db))


@router.delete("/v1/admin/layers/{ident}", status_code=204)
def remove(ident: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = settings_access.require(db, user, "set-layers")
    layer = layers.require_layer(db, ident)
    layer = db.scalar(select(Resource).where(Resource.id == layer.id).with_for_update()
                      .execution_options(populate_existing=True))
    from ..models import PlatformSetting
    setting = db.get(PlatformSetting, layers.DEFAULTS)
    if setting and (setting.value or {}).get("default") == layer.id:
        raise HTTPException(409, "this is the default layer; choose another default layer first")
    if layer.config.get('infrastructure_generation'):
        from ..models import LayerOperation
        current = db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layer.id,
            LayerOperation.generation == layer.config['infrastructure_generation']))
        if not current or current.phase != 'Retired':
            raise HTTPException(409, 'retire this layer first (POST .../infrastructure/retire); its storage is retained')
        if current.reason != 'StorageDeleted':
            raise HTTPException(409, f'retained storage still exists in namespace {current.namespace}; delete that namespace '
                                     'to remove the data permanently, then retry once the controller reports it gone')
    used = any(r.id != layer.id and layers.explicit_layer(db, r) == layer.id for r in db.scalars(select(Resource)))
    used |= any(w.environment_id == layer.id for model in (Workspace, Run) for w in db.scalars(select(model)))
    used |= any((x.config or {}).get("layer_id") == layer.id for model in (DataSource, ApiDefinition) for x in db.scalars(select(model)))
    if used:
        raise HTTPException(409, "layer still contains objects, workspaces or run history")
    rbac.delete_resource_assignments(db,layer.id)
    from ..models import LayerOperation
    # Its provisioning history goes with it (the audit log keeps the deletion).
    db.execute(delete(LayerOperation).where(LayerOperation.layer_id == layer.id))
    from ..cert_monitor import forget
    forget(db, layer.id)
    db.delete(layer)
    audit(db, user.username, "layer.delete", ident)
    db.commit()
