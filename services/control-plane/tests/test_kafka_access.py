import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import jwt
import pytest
from sqlalchemy import select

from app import kafka_access as kafka, layers
from app.config import get_settings
from app.db import SessionLocal
from app.models import KafkaSession, LayerOperation, Resource
from .conftest import as_
from .rbac_helpers import grant_group


@pytest.fixture
def topic(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'layer_namespace_prefix', 'test-layer')
    key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    monkeypatch.setattr(get_settings(), 'trino_jwt_private_key', key)
    spec = {'enabled':True, 'trino':{'workers':{'warehouse':'trino'}}, 'kafka':{
        'enabled':True, 'image':'fdp/kafka:test', 'warehouse':'small',
        'tls':{'mode':'existing', 'secret_name':'kafka-tls'}}}
    out = client.put('/v1/admin/layers/'+layers.NON_PROD+'/infrastructure', headers=as_('admin'),
                     json={'expected_generation':0,'spec':spec})
    assert out.status_code == 202, out.text
    with SessionLocal() as db:
        db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layers.NON_PROD)).phase = 'Ready'
        db.commit()
    out = client.post('/v1/kafka/clusters/'+layers.NON_PROD, headers=as_('admin'))
    assert out.status_code == 201, out.text
    out = client.post('/v1/kafka/topics', headers=as_('admin'), json={'name':'orders', 'layer_id':layers.NON_PROD})
    assert out.status_code == 201, out.text
    with SessionLocal() as db:
        item = db.get(Resource, out.json()['id'])
        item.config = {**item.config, 'phase':'Ready'}
        db.commit()
    return out.json()


def issue(client, ident, mode='consume', user='ana', **extra):
    return client.post('/v1/kafka/topics/'+ident+'/session', headers=as_(user),
                       json={'mode':mode,'group':'billing', **extra})


def decision(client, session, operation, kind='TOPIC', name='orders', layer=layers.NON_PROD, authenticated=True):
    sub = jwt.decode(session['token'], options={'verify_signature':False})['sub']
    body = {'input':{'requestContext':{'principal':{'principalType':'User','name':sub}},
        'action':{'operation':operation,'resourcePattern':{'resourceType':kind,'name':name,'patternType':'LITERAL'}}}}
    return client.post('/internal/kafka/layers/'+layer+'/allow', json=body,
        headers={'Authorization':'Bearer '+kafka.broker_credential(layer)} if authenticated else {})


def test_reader_cannot_produce_or_hijack_consumer_groups_and_revocation_is_live(client, topic):
    grant_group(client, topic['id'], 'Readers', 'Reader', users=('ana',))
    out = issue(client, topic['id'])
    assert out.status_code == 200, out.text
    session = out.json()
    assert session['security_protocol'] == 'SASL_SSL' and session['group_id'].endswith('.billing')
    assert decision(client, session, 'READ').json() == {'result':True}
    assert decision(client, session, 'WRITE').json() == {'result':False}
    assert decision(client, session, 'READ', 'GROUP', session['group_id']).json() == {'result':True}
    assert decision(client, session, 'READ', 'GROUP', 'another.billing').json() == {'result':False}
    assert decision(client, session, 'READ', name='other-topic').json() == {'result':False}
    assert decision(client, session, 'ALTER').json() == {'result':False}
    assert decision(client, session, 'READ', authenticated=False).status_code == 401
    assert decision(client, session, 'READ', layer=layers.PROD).json() == {'result':False}
    assert issue(client, topic['id'], mode='produce').status_code == 403
    with SessionLocal() as db:
        from app.models import RoleAssignment
        db.query(RoleAssignment).filter(RoleAssignment.resource_id == topic['id'], RoleAssignment.role == 'Reader').delete()
        db.commit()
    assert decision(client, session, 'READ').json() == {'result':False}


def test_contributor_can_produce_but_not_modify_topic_or_native_acl(client, topic):
    grant_group(client, topic['id'], 'Writers', 'Contributor', users=('ana',))
    session = issue(client, topic['id'], mode='produce').json()
    assert decision(client, session, 'WRITE').json() == {'result':True}
    assert decision(client, session, 'IDEMPOTENT_WRITE', 'CLUSTER', 'kafka-cluster').json() == {'result':True}
    assert decision(client, session, 'CREATE', 'CLUSTER', 'kafka-cluster').json() == {'result':False}
    assert client.put('/v1/kafka/topics/'+topic['id'], headers=as_('ana'), json={'partitions':4}).status_code == 403
    assert client.delete('/v1/kafka/topics/'+topic['id'], headers=as_('ana')).status_code == 403


def test_expired_or_deleted_topic_sessions_fail_closed(client, topic):
    grant_group(client, topic['id'], 'Readers', 'Reader', users=('ana',))
    session = issue(client, topic['id']).json()
    with SessionLocal() as db:
        row = db.scalar(select(KafkaSession))
        row.expires_at = int(time.time())-1
        db.commit()
    assert decision(client, session, 'READ').json() == {'result':False}
    assert client.delete('/v1/kafka/topics/'+topic['id'], headers=as_('admin')).status_code == 204
    assert issue(client, topic['id']).status_code == 404


def test_topic_only_grant_is_navigable_without_enumerating_other_topics(client, topic):
    grant_group(client, topic['id'], 'Readers', 'Reader', users=('ana',))
    result = client.get('/v1/rbac/resources',headers=as_('ana'))
    clusters = [r for r in result.json() if r['kind']=='kafka']
    assert len(clusters)==1 and not clusters[0]['actions']
    assert not any(r['kind']=='topic' for r in result.json())
    assert client.get('/v1/kafka/clusters/'+clusters[0]['id'],headers=as_('ana')).status_code==200
    assert [r['id'] for r in client.get('/v1/kafka/topics',headers=as_('ana')).json()]==[topic['id']]
    assert client.get('/v1/kafka/clusters/'+clusters[0]['id'],headers=as_('bob')).status_code==403
    assert client.get('/v1/kafka/resolve',params={'topic':'orders'},headers=as_('ana')).json()['id']==topic['id']


def test_malformed_callbacks_fail_closed_and_key_rotation_accepts_previous_broker_key(client, topic, monkeypatch):
    old_key=get_settings().session_signing_key
    monkeypatch.setattr(get_settings(),'session_signing_key','new-key-that-is-at-least-32-characters')
    monkeypatch.setattr(get_settings(),'session_signing_previous_keys',old_key)
    headers={'Authorization':'Bearer '+kafka.broker_credential(layers.NON_PROD,old_key)}
    for value in ([], {'requestContext':'bad'}, {'action':[]}, {'requestContext':{'principal':[]}}):
        result=client.post('/internal/kafka/layers/'+layers.NON_PROD+'/allow',headers=headers,json={'input':value})
        assert result.status_code==200 and result.json()=={'result':False}
    monkeypatch.setattr(get_settings(),'session_signing_previous_keys','')
    assert client.post('/internal/kafka/layers/'+layers.NON_PROD+'/allow',headers=headers,json={}).status_code==401


def test_recreating_topic_name_does_not_restore_old_grants_or_sessions(client, topic):
    grant_group(client,topic['id'],'Readers','Reader',users=('ana',))
    session=issue(client,topic['id']).json()
    assert client.delete('/v1/kafka/topics/'+topic['id'],headers=as_('admin')).status_code==204
    assert client.post('/v1/kafka/topics',headers=as_('admin'),json={'name':'orders','layer_id':layers.NON_PROD}).status_code==409
    with SessionLocal() as db:
        old=db.get(Resource,topic['id']);old.config={**old.config,'phase':'Deleted'};db.commit()
    replacement=client.post('/v1/kafka/topics',headers=as_('admin'),json={'name':'orders','layer_id':layers.NON_PROD})
    assert replacement.status_code==201,replacement.text
    assert replacement.json()['id']!=topic['id']
    assert decision(client,session,'READ').json()=={'result':False}
    assert client.get('/v1/kafka/topics/'+replacement.json()['id'],headers=as_('ana')).status_code==403


def test_topic_lifecycle_validation_and_inherited_grants(client, topic):
    with SessionLocal() as db:
        cluster_id = db.get(Resource, topic['id']).parent_id
    grant_group(client, cluster_id, 'Inherited readers', 'Reader', users=('ana',))
    assert issue(client, topic['id']).status_code == 200
    assert client.post('/v1/kafka/topics', headers=as_('admin'),
                       json={'name':'orders', 'layer_id':layers.NON_PROD}).status_code == 409
    assert client.post('/v1/kafka/topics', headers=as_('admin'),
                       json={'name':'__internal', 'layer_id':layers.NON_PROD}).status_code == 422
    assert client.put('/v1/kafka/topics/'+topic['id'], headers=as_('admin'), json={'partitions':2}).status_code == 422
    assert client.put('/v1/kafka/topics/'+topic['id'], headers=as_('admin'), json={'partitions':4}).status_code == 200
