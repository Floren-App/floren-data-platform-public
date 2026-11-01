"""Governed topic lifecycle, native client sessions and authenticated broker callbacks."""
from typing import Any, Dict, Literal
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .. import kafka_access as kafka, layers, rbac
from ..db import get_db
from ..deps import get_actor
from ..models import Resource
from ..ownership import browsing_allowed
from .common import audit
from .rbac import admin, resource_out

router = APIRouter(tags=['kafka'])


class TopicSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    partitions: int = Field(default=3, ge=1, le=10000)
    replication_factor: int = Field(default=1, ge=1, le=5)
    retention_ms: int = Field(default=604800000, ge=60000, le=31536000000)
    cleanup_policy: Literal['delete', 'compact', 'compact,delete'] = 'delete'


class TopicIn(TopicSettings):
    layer_id: str = Field(pattern=r'^[a-f0-9]{12}$')
    name: str = Field(min_length=1, max_length=249, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


class SessionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['produce', 'consume']
    group: str = Field(default='', max_length=180, pattern=r'^[A-Za-z0-9._-]*$')


@router.post('/v1/kafka/clusters/{layer_id}', status_code=201)
def register_cluster(layer_id: str, db=Depends(get_db), user=Depends(get_actor)):
    user = admin(db, user)
    from ..ownership import require_create
    require_create(db, user)
    layer, spec = kafka.specification(db, layer_id, require_running=False)
    item = kafka.cluster(db, layer.id)
    if not item:
        item = Resource(kind='kafka', name=layer.name+' Kafka', object_id=layer.id,
                        parent_id=layer.id, config={'layer_id':layer.id, 'managed':True})
        db.add(item)
        db.flush()
        rbac.own(db, user, item)
        audit(db, user.username, 'kafka.cluster.register', item.id)
        db.commit()
    elif item.config.get('deleted'):
        item.config = {**item.config, 'deleted':False}
        audit(db, user.username, 'kafka.cluster.register', item.id)
        db.commit()
    return resource_out(db, item, user)


@router.get('/v1/kafka/clusters')
def clusters(db=Depends(get_db), user=Depends(get_actor)):
    return [resource_out(db, r, user) for r in db.scalars(select(Resource).where(Resource.kind == 'kafka'))
            if not r.config.get('deleted') and browsing_allowed(db, user, r)]


@router.get('/v1/kafka/topics')
def topics(layer_id: str = '', db=Depends(get_db), user=Depends(get_actor)):
    return [resource_out(db, r, user) for r in db.scalars(select(Resource).where(Resource.kind == 'topic'))
            if not r.config.get('deleted') and (not layer_id or layers.resource_layer(db, r) == layer_id)
            and browsing_allowed(db, user, r)]


@router.get('/v1/kafka/clusters/{ident}')
def get_cluster(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    item = db.get(Resource, ident)
    if not item or item.kind != 'kafka' or item.config.get('deleted'):
        raise HTTPException(404, 'Kafka integration not found')
    if not rbac.allowed(db, user, 'read', item) and not any(
            not t.config.get('deleted') and browsing_allowed(db, user, t)
            for t in db.scalars(select(Resource).where(Resource.kind == 'topic', Resource.parent_id == ident))):
        rbac.require(db, user, 'read', item)
    result = resource_out(db, item, user)
    layer = layers.require_layer(db, item.object_id)
    infrastructure = layer.config.get('infrastructure') or {}
    settings = (infrastructure.get('kafka') or {})
    brokers = settings.get('replicas', 1)
    return {**result, 'config':{**result['config'], 'broker_count':brokers,
                              'retired':bool(infrastructure.get('retired')),
                              'running':bool(settings.get('running', settings.get('enabled', False))),
                              'configured':bool(settings.get('image') and settings.get('warehouse'))}}


@router.delete('/v1/kafka/clusters/{ident}', status_code=204)
def unregister_cluster(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    user = admin(db, user)
    item = db.get(Resource, ident)
    if not item or item.kind != 'kafka':
        raise HTTPException(404, 'Kafka integration not found')
    raise HTTPException(409, 'Kafka is a built-in layer capability; stop its brokers instead of removing it')


@router.get('/v1/kafka/resolve')
def resolve_topic(topic: str, layer_id: str = '', db=Depends(get_db), user=Depends(get_actor)):
    candidates = db.scalars(select(Resource).where(Resource.kind == 'topic',
        (Resource.id == topic) | (Resource.name == topic)))
    matches = [r for r in candidates if not r.config.get('deleted') and
        (not layer_id or layers.resource_layer(db, r) == layer_id) and rbac.allowed(db, user, 'read', r)]
    if len(matches) != 1:
        raise HTTPException(404, 'Choose one accessible Kafka topic by ID, or specify its layer')
    return resource_out(db, matches[0], user)


@router.get('/v1/kafka/topics/{ident}')
def get_topic(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    item = kafka.topic(db, ident)
    rbac.require(db, user, 'read', item)
    return resource_out(db, item, user)


@router.post('/v1/kafka/topics', status_code=201)
def create_topic(body: TopicIn, db=Depends(get_db), user=Depends(get_actor)):
    _, spec = kafka.specification(db, body.layer_id)
    cluster = kafka.cluster(db, body.layer_id)
    if not cluster or cluster.config.get('deleted'):
        raise HTTPException(409, 'Register the layer Kafka integration first')
    rbac.require(db, user, 'ddl', cluster)
    from ..ownership import require_create
    require_create(db, user, cluster, 'ddl')
    db.scalar(select(Resource).where(Resource.id == cluster.id).with_for_update())
    if body.name in ('.', '..') or body.name.startswith('__'):
        raise HTTPException(422, 'Reserved Kafka topic name')
    if body.replication_factor > spec.kafka.replicas:
        raise HTTPException(422, 'Replication factor exceeds the broker count')
    if spec.kafka.replicas > 1 and body.replication_factor < 3:
        raise HTTPException(422, 'Multi-broker clusters require a replication factor of at least 3')
    previous = db.scalar(select(Resource).where(Resource.kind == 'topic', Resource.parent_id == cluster.id,
                                               Resource.name == body.name).with_for_update())
    if previous:
        if not previous.config.get('deleted') or previous.config.get('phase') != 'Deleted':
            raise HTTPException(409, 'This topic already exists or is pending deletion')
        # Recreating a name is a new security boundary: retain the old object/grants
        # for history, but never reactivate its sessions or give old grantees new data.
        previous.config = {**previous.config, 'original_name':previous.name}
        previous.name = previous.name[:230]+'.retired.'+previous.id
    item = Resource(kind='topic', name=body.name, parent_id=cluster.id,
                    config={**body.model_dump(exclude={'name'}), 'phase':'Pending'})
    db.add(item)
    db.flush()
    rbac.own(db, user, item)
    audit(db, user.username, 'kafka.topic.create', item.id, layer_id=body.layer_id)
    db.commit()
    return resource_out(db, item, user)


@router.put('/v1/kafka/topics/{ident}')
def update_topic(ident: str, body: TopicSettings, db=Depends(get_db), user=Depends(get_actor)):
    item = kafka.topic(db, ident, locked=True)
    rbac.require(db, user, 'ddl', item)
    _, spec = kafka.specification(db, layers.resource_layer(db, item))
    if body.partitions < item.config['partitions']:
        raise HTTPException(422, 'Kafka partitions cannot be decreased')
    if body.replication_factor != item.config['replication_factor']:
        raise HTTPException(422, 'Replica reassignment requires a controlled broker operation')
    item.config = {**item.config, **body.model_dump(), 'phase':'Pending'}
    audit(db, user.username, 'kafka.topic.update', item.id)
    db.commit()
    return resource_out(db, item, user)


@router.delete('/v1/kafka/topics/{ident}', status_code=204)
def delete_topic(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    item = kafka.topic(db, ident, locked=True)
    rbac.require(db, user, 'ddl', item)
    kafka.specification(db, layers.resource_layer(db, item))
    item.config = {**item.config, 'deleted':True, 'phase':'Deleting'}
    audit(db, user.username, 'kafka.topic.delete', item.id, topic_name=item.name)
    db.commit()


@router.get('/v1/kafka/topics/{ident}/deletion')
def deletion(ident: str, db=Depends(get_db), user=Depends(get_actor)):
    item = db.get(Resource, ident)
    if not item or item.kind != 'topic':
        raise HTTPException(404, 'Kafka topic not found')
    rbac.require(db, user, 'ddl', item)
    return {'phase':item.config.get('phase', 'Pending')}


@router.post('/v1/kafka/topics/{ident}/session')
def session(ident: str, body: SessionIn, db=Depends(get_db), user=Depends(get_actor)):
    item = kafka.topic(db, ident)
    result = kafka.issue(db, user, item, body.mode, body.group)
    audit(db, user.username, 'kafka.session.issue', item.id, mode=body.mode)
    db.commit()
    return result


@router.post('/v1/kafka/topics/{ident}/observations', status_code=201)
def observation(ident: str, body: SessionIn, db=Depends(get_db), user=Depends(get_actor)):
    item = kafka.topic(db, ident)
    rbac.require(db, user, 'write' if body.mode == 'produce' else 'read', item)
    from .. import catalog, measurements
    from ..models import Run, Workload, utcnow
    asset = catalog.upsert(db, item, kind='topic', attributes={'layer_id':item.config['layer_id']})
    workload, run = None, None
    kind, _, context_id = user.execution_context.partition(':')
    if kind == 'run':
        run = db.get(Run, context_id)
        workload = db.get(Workload, run.workload_id) if run and run.workload_id else None
    elif kind == 'workload':
        workload = db.get(Workload, context_id)
    if workload:
        resource = rbac.workload_resource(db, workload)
        if layers.resource_layer(db, resource) != item.config['layer_id']:
            raise HTTPException(403, 'Cross-layer Kafka lineage is forbidden')
        program = catalog.upsert(db, resource, kind=workload.kind)
        source, target = (program, asset) if body.mode == 'produce' else (asset, program)
        catalog.edge(db, resource, source, target, 'writes' if body.mode == 'produce' else 'reads',
                     'kafka_runtime', run.commit_sha if run else workload.deployed_sha or '')
    if body.mode == 'produce':
        at = utcnow()
        measurements.dataset_observation(db, asset, at, 'kafka_sdk', None, 'refresh',
                                         run.id if run else '', run.commit_sha if run else '')
        asset.last_success_at = at
    db.commit()
    return {'recorded':True}


@router.get('/internal/kafka/jwks', include_in_schema=False)
def jwks():
    from ..sql_access import layer_jwks
    return layer_jwks()


@router.post('/internal/kafka/layers/{layer_id}/allow', include_in_schema=False)
def allow(layer_id: str, body: Dict[str, Any], authorization: str = Header(default=''), db=Depends(get_db)):
    from ..auth import session_keys
    matches = [hmac.compare_digest(authorization, 'Bearer '+kafka.broker_credential(layer_id, key))
               for key in session_keys()]
    if not any(matches):
        raise HTTPException(401, 'Broker authentication required')
    try:
        permitted = kafka.decide(db, layer_id, body.get('input') or {})
    except (HTTPException, ValueError, TypeError, KeyError, AttributeError):
        permitted = False
    # Never audit tokens, message payloads or untrusted request bodies.
    payload = body.get('input')
    action = payload.get('action') if isinstance(payload, dict) else {}
    action = action if isinstance(action, dict) else {}
    audit(db, 'kafka-broker', 'kafka.authorize', layer_id,
          operation=str(action.get('operation', ''))[:64], allowed=bool(permitted))
    db.commit()
    return {'result':bool(permitted)}


class KafkaRunning(BaseModel):
    running: bool


@router.put('/v1/kafka/clusters/{ident}/running', status_code=202)
def set_running(ident: str, body: KafkaRunning, db=Depends(get_db), user=Depends(get_actor)):
    from .. import layer_infrastructure as infrastructure, settings_access
    item = db.get(Resource, ident)
    if not item or item.kind != 'kafka':
        raise HTTPException(404, 'Kafka cluster not found')
    layer = layers.require_layer(db, item.object_id)
    user = settings_access.require(db, user, 'set-layers', 'layer_infrastructure.apply', layer_id=layer.id)
    current = infrastructure.state(db, layer)
    if body.running and current['spec']['retired']:
        raise HTTPException(409, 'Reactivate the layer before starting Kafka brokers')
    from pydantic import ValidationError
    try:
        spec = infrastructure.InfrastructureSpec.model_validate({**current['spec'],
            'kafka':{**current['spec']['kafka'], 'running':body.running}})
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from None
    result = infrastructure.configure(db, layer.id, infrastructure.InfrastructureChange(
        expected_generation=current['generation'], spec=spec))
    audit(db, user.username, 'kafka.brokers.running', item.id, running=body.running)
    db.commit()
    return result
