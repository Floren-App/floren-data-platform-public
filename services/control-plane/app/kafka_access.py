"""Topic IAM and short-lived, layer/topic-scoped native Kafka sessions."""
import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException
import jwt
from sqlalchemy import delete, select

from . import layers, rbac
from .auth import User
from .config import get_settings
from .layer_infrastructure import InfrastructureSpec, require_ready
from .models import KafkaSession, Resource, Run, Workspace, Workload


def broker_credential(layer_id, key=None):
    key = get_settings().session_signing_key if key is None else key
    if not key or len(key) < 32 or key == 'dev-only-change-me':
        raise HTTPException(503, 'Configure the platform signing key before enabling Kafka')
    return hmac.new(key.encode(), ('fdp-kafka-policy-v1:'+layer_id).encode(), hashlib.sha256).hexdigest()


def specification(db, layer_id, require_running=True):
    layer = layers.require_layer(db, layer_id)
    spec = InfrastructureSpec.model_validate((layer.config or {}).get('infrastructure', {}))
    if require_running and (not spec.enabled or not spec.kafka.running or spec.retired):
        raise HTTPException(409, 'Kafka brokers are stopped in this layer; start them through layer infrastructure')
    return layer, spec


def cluster(db, layer_id):
    return db.scalar(select(Resource).where(Resource.kind == 'kafka', Resource.object_id == layer_id))


def topic(db, ident, locked=False):
    item = db.scalar(select(Resource).where(Resource.id == ident).with_for_update()) if locked else db.get(Resource, ident)
    if not item or item.kind != 'topic' or item.config.get('deleted'):
        raise HTTPException(404, 'Kafka topic not found')
    parent = db.get(Resource, item.parent_id)
    if not parent or parent.config.get('deleted'):
        raise HTTPException(404, 'Kafka integration not found')
    return item


def session_active(db, session):
    if session.expires_at <= int(time.time()):
        return False
    kind, _, ident = session.execution_context.partition(':')
    if kind == 'workspace':
        item = db.get(Workspace, ident)
        return item is not None and item.status == 'running'
    if kind == 'run':
        item = db.get(Run, ident)
        return item is not None and item.status in ('running', 'pending')
    if kind == 'workload':
        item = db.get(Workload, ident)
        return item is not None and item.status in ('live', 'deploying') and item.identity == session.username
    return kind in ('', 'service-token')


def issue(db, actor, item, mode, group=''):
    actor = rbac.resolve_user(db, actor)
    layer_id = layers.resource_layer(db, item)
    _, spec = specification(db, layer_id)
    require_ready(db, layer_id)
    action = 'write' if mode == 'produce' else 'read'
    rbac.require(db, actor, action, item)
    if item.config.get('phase') != 'Ready':
        raise HTTPException(409, 'The Kafka topic is not ready; check its provisioning status')
    if not actor.principal_id:
        raise HTTPException(401, 'Kafka requires a provisioned directory identity')
    if mode == 'consume' and not group:
        raise HTTPException(422, 'A consumer group is required')
    own_group = actor.principal_id+'.'+group if group else ''
    expires = int(time.time()) + spec.kafka.token_ttl_seconds
    record = KafkaSession(id='s_'+secrets.token_urlsafe(24), principal_id=actor.principal_id,
        username=actor.username, layer_id=layer_id, topic_id=item.id, mode=mode,
        consumer_group=own_group, execution_context=actor.execution_context, expires_at=expires)
    db.add(record)
    # Bounded expiry cleanup; no long-running periodic full-table scan on admissions.
    expired = list(db.scalars(select(KafkaSession.id).where(KafkaSession.expires_at < int(time.time()))
                              .order_by(KafkaSession.expires_at).limit(100)))
    if expired:
        db.execute(delete(KafkaSession).where(KafkaSession.id.in_(expired)))
    from .sql_access import _layer_key, _key_id
    key = _layer_key()
    bearer = jwt.encode({'sub':record.id, 'typ':'Bearer', 'iss':'fdp-kafka', 'aud':'fdp-kafka:'+layer_id,
                         'iat':int(time.time()), 'exp':expires}, key, algorithm='ES256',
                        headers={'kid':_key_id(key.public_key())})
    from .layer_infrastructure import placement
    placed = placement(db, layer_id)
    return {'token':bearer, 'expires_at':expires, 'topic':item.name,
            'bootstrap_servers':f'fdp-kafka-kafka-bootstrap.{placed.namespace}.svc:9093',
            'security_protocol':'SASL_SSL', 'sasl_mechanism':'OAUTHBEARER',
            'group_id':own_group or None, 'layer_id':layer_id}


def decide(db, layer_id, payload):
    """OPA-shaped broker request. Never trust a caller-supplied plain principal ID."""
    if not isinstance(payload, dict):
        return False
    context = payload.get('requestContext') or {}
    if not isinstance(context, dict):
        return False
    principal = context.get('principal') or {}
    if not isinstance(principal, dict) or principal.get('principalType') != 'User':
        return False
    name = principal.get('name')
    if not isinstance(name, str) or not name.startswith('s_') or len(name) > 64:
        return False
    action = payload.get('action') or {}
    if not isinstance(action, dict):
        return False
    pattern = action.get('resourcePattern') or {}
    if not isinstance(pattern, dict):
        return False
    session = db.get(KafkaSession, name)
    if not session or session.layer_id != layer_id or not session_active(db, session):
        return False
    specification(db, layer_id)
    item = topic(db, session.topic_id)
    if layers.resource_layer(db, item) != layer_id:
        return False
    actor = rbac.resolve_user(db, User(session.username, principal_id=session.principal_id,
        environment_id=layer_id, execution_context=session.execution_context or 'kafka-session'))
    if pattern.get('unknown') or pattern.get('patternType') != 'LITERAL':
        return False
    operation = action.get('operation')
    kind, name = pattern.get('resourceType'), pattern.get('name')
    if kind == 'TOPIC' and name == item.name:
        if operation in ('READ', 'DESCRIBE', 'DESCRIBE_CONFIGS'):
            return rbac.allowed(db, actor, 'read', item) and (operation != 'READ' or session.mode == 'consume')
        if operation == 'WRITE' and session.mode == 'produce':
            return rbac.allowed(db, actor, 'write', item)
    if kind == 'GROUP' and session.mode == 'consume' and name == session.consumer_group:
        return operation in ('READ', 'DESCRIBE') and rbac.allowed(db, actor, 'read', item)
    if kind == 'CLUSTER' and name == 'kafka-cluster' and operation == 'IDEMPOTENT_WRITE':
        return session.mode == 'produce' and rbac.allowed(db, actor, 'write', item)
    if kind == 'CLUSTER' and name == 'kafka-cluster' and operation == 'DESCRIBE':
        return rbac.allowed(db, actor, 'read', item)
    # Native DDL and ACL/cluster administration are deliberately not granted by a data session.
    # Topic lifecycle uses the validated FDP API and its controller-owned KafkaTopic CRs.
    return False
