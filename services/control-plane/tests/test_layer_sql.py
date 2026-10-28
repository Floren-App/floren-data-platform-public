"""SQL follows the data's layer to that layer's Trino; contexts never cross engines."""
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from app import layers, sql_access
from app.config import get_settings
from app.db import SessionLocal
from app.models import DataSource, LayerOperation, Resource, SqlQuery
from .conftest import as_
from .rbac_helpers import grant_group, integration
from .test_layer_infrastructure import managed
from .test_shared_sql import sql_mode  # noqa: F401

PREFIX = 'dc-test-layer'


@pytest.fixture
def layered(sql_mode, monkeypatch):  # noqa: F811
    client, execution = sql_mode
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
    monkeypatch.setattr(get_settings(), 'trino_jwt_private_key', pem)
    monkeypatch.setattr(get_settings(), 'layer_namespace_prefix', PREFIX)
    # A Prod catalog Ana can also read: it must never appear in a Non-Prod engine scope.
    client.put('/v1/admin/datasources/prodlake', headers=as_('admin'),
               json=integration('prodlake', 'postgres', layer_id=layers.PROD)).raise_for_status()
    schema = client.post('/v1/rbac/sources/prodlake/schemas', headers=as_('admin'), json={'name': 'reporting'}).json()
    grant_group(client, schema['id'], 'prodlake-readers', 'Reader', users=('ana',))
    with SessionLocal() as db:
        resource = db.get(Resource, schema['id'])
        resource.config = {**resource.config, 'state': 'ready'}
        db.commit()
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    assert client.put(path, headers=as_('admin'), json={'expected_generation': 0, 'spec': managed()}).status_code == 202
    sent = []

    async def engine(method, path, *, headers, query, content=None):
        sent.append({'method': method, 'url': sql_access.engine_url(query) + path, 'headers': headers})
        return httpx.Response(200, json={'id': 'engine_q', 'stats': {'state': 'FINISHED'}, 'columns': [], 'data': []})

    monkeypatch.setattr(sql_access, 'engine_request', engine)
    return client, execution, sent


def ready(layer=layers.NON_PROD):
    with SessionLocal() as db:
        operation = db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layer))
        operation.phase = 'InfrastructureReady'
        db.commit()


def test_query_waits_for_layer_engine_then_routes_with_layer_token(layered):
    client, _, sent = layered
    body = {'source': 'finance', 'sql': 'SELECT 1'}
    blocked = client.post('/v1/sql/query', headers=as_('ana'), json=body)
    assert blocked.status_code == 409 and blocked.json()['detail']['code'] == 'LayerNotReady'
    assert sent == []
    ready()
    assert client.post('/v1/sql/query', headers=as_('ana'), json=body).status_code == 200
    assert sent[0]['url'] == 'http://fdp-trino.%s-%s.svc:8080/v1/statement' % (PREFIX, layers.NON_PROD)
    token = sent[0]['headers']['Authorization'].split(' ', 1)[1]
    assert jwt.get_unverified_header(token)['alg'] == 'ES256'
    assert jwt.decode(token, options={'verify_signature': False})['aud'] == 'trino-layer-' + layers.NON_PROD
    with SessionLocal() as db:
        query = db.scalar(select(SqlQuery))
        assert query.engine_layer == layers.NON_PROD
        # Prod data is outside this engine even though Ana is granted it.
        assert 'prodlake' not in query.scope and 'finance' in query.scope


def test_unmanaged_layer_keeps_shared_engine(layered):
    client, _, sent = layered
    assert client.post('/v1/sql/query', headers=as_('ana'), json={'source': 'prodlake', 'sql': 'SELECT 1'}).status_code == 200
    assert sent[0]['url'].startswith(get_settings().trino_url.rstrip('/') + '/v1/statement')
    assert jwt.decode(sent[0]['headers']['Authorization'].split(' ', 1)[1], options={'verify_signature': False})['aud'] == 'trino'


def test_policy_context_is_only_valid_on_its_own_engine(layered):
    client, _, sent = layered
    ready()
    assert client.post('/v1/sql/query', headers=as_('ana'), json={'source': 'finance', 'sql': 'SELECT 1'}).status_code == 200
    with SessionLocal() as db:
        query = db.scalar(select(SqlQuery))
        context = sent[0]['headers']['X-Trino-Extra-Credential'].split('=', 1)[1]
        request = {'input': {'context': {'identity': {'user': query.principal_id, 'extraCredentials': {'dc_context': context}}},
                             'action': {'operation': 'SelectFromColumns', 'resource': {'table': {
                                 'catalogName': 'finance', 'schemaName': 'reporting', 'tableName': 't'}}}}}
        with SessionLocal() as other:
            other.get(SqlQuery, query.id).state = 'RUNNING'
            other.commit()
    own = client.post('/internal/trino/layers/%s/allow' % layers.NON_PROD, json=request).json()
    assert own == {'result': True}
    assert client.post('/internal/trino/layers/%s/allow' % layers.PROD, json=request).json() == {'result': False}
    assert client.post('/internal/trino/allow', json=request).json() == {'result': False}
    assert client.post('/internal/trino/layers/%s/batch' % layers.PROD, json={'input': {**request['input'], 'action': {
        'operation': 'FilterCatalogs', 'filterResources': [{'catalog': {'name': 'finance'}}]}}}).json() == {'result': []}


def test_workload_cannot_pull_another_layers_catalog_into_its_engine(layered):
    client, execution, sent = layered
    ready()
    # The workspace is bound to Non-Prod; a Prod catalog on the managed engine is refused.
    response = client.post('/v1/statement', headers={**execution, 'X-Trino-Catalog': 'prodlake'}, content='SELECT 1')
    assert response.status_code == 403, response.text
    assert 'own layer' in response.text
    assert sent == []


def test_platform_metadata_never_reaches_a_layer_engine(layered):
    with SessionLocal() as db:
        finance = db.scalar(select(DataSource).where(DataSource.name == 'finance'))
        scope = {'fdp_metadata': {'source_id': finance.id, 'system_metadata': True, 'schemas': {'metadata': 'Reader'}},
                 'finance': {'source_id': finance.id, 'schemas': {'reporting': 'Reader'}}}
        assert list(sql_access.layer_scope(db, scope, layers.NON_PROD)) == ['finance']
        assert sql_access.layer_scope(db, scope, layers.PROD) == {}
