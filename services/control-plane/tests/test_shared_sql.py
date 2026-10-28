import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import rbac, sql_access
from app.auth import User, mint_token
from app.config import get_settings
from app.db import SessionLocal
from app.models import DataSource, Resource, SqlQuery, Workspace, utcnow
from .conftest import as_
from .rbac_helpers import grant_group, integration


@pytest.fixture
def sql_mode(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "query_executor", "trino")
    monkeypatch.setattr(s, "trino_signing_key", "dev-only-trino-key-change-me-01234567890123456789")
    monkeypatch.setattr(sql_access, 'check_native_relations', lambda query: None)
    for name, kind in (("finance", "postgres"), ("lake", "iceberg")):
        client.put('/v1/admin/datasources/' + name, headers=as_('admin'),
                   json=integration(name, kind)).raise_for_status()
        schema = client.post('/v1/rbac/sources/' + name + '/schemas', headers=as_('admin'), json={'name': 'reporting'}).json()
        grant_group(client, schema['id'], name + '-readers', 'Reader', users=('ana',))
        with SessionLocal() as db:
            resource = db.get(Resource, schema['id'])
            resource.config = {**resource.config, 'state': 'ready'}
            db.commit()
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
        ws = Workspace(name='SQL test', owner='ana', principal_id=ana.principal_id, editor='vscode', status='running')
        db.add(ws)
        db.commit()
        execution = mint_token('execution', 'ana', 3600, ws=ws.id, pid=ana.principal_id)
    return client, {'Authorization': 'Bearer ' + execution}


def test_standard_protocol_strips_identity_and_binds_pages(sql_mode, monkeypatch):
    client, headers = sql_mode
    sent = []

    async def engine(method, path, **kwargs):
        sent.append((method, path, kwargs))
        if method == 'DELETE':
            return httpx.Response(204)
        return httpx.Response(200, json={'id': 'test_query', 'stats': {'state': 'RUNNING'},
            'nextUri': 'http://coordinator:8080/v1/statement/executing/test_query/token/1',
            'infoUri': 'http://coordinator:8080/ui/query.html?test_query'})

    monkeypatch.setattr(sql_access, 'engine_request', engine)
    response = client.post('/v1/statement', headers={**headers, 'X-Trino-User': 'admin',
        'X-Trino-Extra-Credential': 'dc_context=forged', 'X-Trino-Catalog': 'finance'}, content='SELECT 1')
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        query = db.scalar(select(SqlQuery))
        assert sent[0][2]['headers']['X-Trino-User'] == query.principal_id
        assert sent[0][2]['headers']['X-Trino-Extra-Credential'] != 'dc_context=forged'
    uri = response.json()['nextUri']
    assert '/v1/sql/pages/' in uri and 'coordinator' not in uri
    assert client.get(uri, headers=as_('bob')).status_code == 404
    assert client.get(uri, headers=headers).status_code == 200
    assert client.delete(uri, headers=headers).status_code == 204
    assert client.post('/v1/statement', headers={**headers, 'X-Trino-Session': 'lake.register_table_procedure_enabled=true'}, content='SELECT 1').status_code == 403


@pytest.mark.parametrize('role,operation,expected', [
    ('Reader', 'SelectFromColumns', True), ('Reader', 'InsertIntoTable', False),
    ('Contributor', 'UpdateTableColumns', True), ('Contributor', 'CreateTable', False),
    ('Admin', 'CreateTable', True), ('Admin', 'SetTableAuthorization', False),
    ('Owner', 'ExecuteProcedure', False), ('Owner', 'CreateViewWithSelectFromColumns', False),
    ('Owner', 'UnknownFutureOperation', False),
])
def test_policy_role_matrix(role, operation, expected):
    query = SqlQuery(scope={'finance': {'schemas': {'reporting': role}}})
    assert sql_access.policy_allow(query, {'operation': operation, 'resource': {
        'table': {'catalogName': 'finance', 'schemaName': 'reporting', 'tableName': 'sales'}}}) is expected


@pytest.mark.parametrize('role', ['Contributor', 'Admin', 'Owner'])
@pytest.mark.parametrize('operation', ['DeleteFromTable','UpdateTableColumns','TruncateTable','DropTable',
                                     'RenameTable','DropSchema','SetTableProperties','RefreshMaterializedView'])
def test_append_only_schemas_reject_mutation_at_engine_boundary(role, operation):
    query = SqlQuery(scope={'lake': {'schemas': {'raw': role}, 'append_only_schemas': ['raw']}})
    resource = {'table': {'catalogName':'lake','schemaName':'raw','tableName':'records'}}
    assert not sql_access.policy_allow(query, {'operation':operation,'resource':resource})
    assert sql_access.policy_allow(query, {'operation':'InsertIntoTable','resource':resource})


def test_table_cannot_be_renamed_into_an_append_only_schema():
    query = SqlQuery(scope={'lake': {'schemas': {'raw':'Admin','history':'Admin'}, 'append_only_schemas':['raw']}})
    assert not sql_access.policy_allow(query, {'operation':'RenameTable',
        'resource':{'table':{'catalogName':'lake','schemaName':'history','tableName':'replacement'}},
        'targetResource':{'table':{'catalogName':'lake','schemaName':'raw','tableName':'records'}}})


def test_append_only_initial_create_is_query_local_and_cannot_replace_data():
    from fastapi import HTTPException
    query = SqlQuery(scope={'lake': {'schemas': {'raw':'Admin'}, 'append_only_schemas':['raw']}})
    action = {'operation':'CreateTable','resource':{'table':{'catalogName':'lake','schemaName':'raw','tableName':'records'}}}
    assert not sql_access.policy_allow(query,action)
    sql_access.authorize_initial_creates(query,'CREATE TABLE IF NOT EXISTS raw.records (id varchar)',catalog='lake')
    assert sql_access.policy_allow(query,action)
    for statement in ["CREATE OR REPLACE TABLE raw.records (id varchar)",
                      "EXECUTE IMMEDIATE 'CREATE OR REPLACE TABLE raw.records AS SELECT 1 AS id'",
                      'PREPARE dangerous FROM CREATE OR REPLACE TABLE raw.records (id varchar)']:
        with pytest.raises(HTTPException):
            sql_access.authorize_initial_creates(query,statement,catalog='lake')


def test_append_only_schema_setup_uses_schema_authorization():
    query = SqlQuery(scope={'lake': {'schemas': {'raw':'Admin'},
        'append_only_schemas':['raw'], 'create_schemas':['raw']}})
    sql_access.authorize_initial_creates(query, 'CREATE SCHEMA IF NOT EXISTS raw',
                                         catalog='lake', schema='raw')
    assert sql_access.policy_allow(query, {'operation':'CreateSchema',
        'resource':{'schema':{'catalogName':'lake','schemaName':'raw'}}})
    assert 'append_only_creates' not in query.scope['lake']
    assert not sql_access.policy_allow(query, {'operation':'CreateTable',
        'resource':{'table':{'catalogName':'lake','schemaName':'raw','tableName':'records'}}})


def test_registered_append_only_contract_is_in_admission_scope(sql_mode):
    client, _ = sql_mode
    response = client.post('/v1/rbac/sources/lake/schemas', headers=as_('admin'),
                           json={'name':'pokemon_raw','append_only':True})
    assert response.status_code == 201, response.text
    resource = response.json()
    assert client.post('/v1/rbac/resources/'+resource['id']+'/provision', headers=as_('admin')).status_code == 200
    with SessionLocal() as db:
        assert db.get(Resource,resource['id']).config['append_only'] is True
        user = rbac.resolve_user(db,User('admin',roles=['fdp-admin']))
        scope = sql_access.scopes(db,sql_access.SqlActor(user,'test'))
        assert 'pokemon_raw' in scope['lake']['append_only_schemas']


def test_policy_locations_targets_metadata_and_unsigned_context(sql_mode):
    client, headers = sql_mode
    query = SqlQuery(scope={'lake': {'schemas': {'reporting': 'Owner', 'private': 'Reader'}}})
    table = {'catalogName': 'lake', 'schemaName': 'reporting', 'tableName': 'sales'}
    assert not sql_access.policy_allow(query, {'operation': 'CreateTable', 'resource': {'table': {
        **table, 'properties': {'location': 's3://lake/private/sales'}}}})
    assert not sql_access.policy_allow(query, {'operation': 'RenameTable', 'resource': {'table': table},
        'targetResource': {'table': {**table, 'schemaName': 'private'}}})
    assert not sql_access.policy_allow(query, {'operation': 'FilterSchemas', 'resource': {
        'schema': {'catalogName': 'lake', 'schemaName': 'missing'}}})
    assert client.post('/internal/trino/allow', json={'input': {'context': {'identity': {
        'user': 'admin', 'extraCredentials': {'dc_context': 'forged'}}}, 'action': {'operation': 'ExecuteQuery'}}}).json() == {'result': False}


def test_connection_has_only_gateway_credentials_and_catalog_uniqueness(sql_mode):
    client, headers = sql_mode
    response = client.get('/v1/connect/finance', headers=headers)
    assert response.status_code == 200, response.text
    info = response.json()
    assert info['kind'] == 'trino' and info['config']['catalog'] == 'finance'
    assert set(info['credentials']) == {'username', 'token'}
    assert 'password' not in info['credentials']
    assert client.put('/v1/admin/datasources/alias', headers=as_('admin'),
                      json=integration('alias', 'postgres', catalog='finance')).status_code == 409
    # The lake integration reserves its warehouse bucket for tables.
    raw = client.put('/v1/admin/datasources/raw-lake', headers=as_('admin'), json=integration('raw-lake', 's3', bucket='lake'))
    assert raw.status_code == 422 and raw.json()['detail']['errors'] == {'bucket': 'this bucket holds Iceberg tables'}


def test_view_security_cannot_be_hidden_in_prepared_wrappers():
    from fastapi import HTTPException
    from urllib.parse import quote_plus
    unsafe = 'CREATE VIEW lake.reporting.v SECURITY DEFINER AS SELECT 1'
    for statement, prepared in [
        (unsafe, ''),
        ('CREATE VIEW lake.reporting.v AS SELECT 1', ''),
        ('PREPARE v FROM ' + unsafe, ''),
        ("EXECUTE IMMEDIATE '" + unsafe + "'", ''),
        ('EXECUTE v', 'v=' + quote_plus(unsafe)),
        ('EXPLAIN ANALYZE ' + unsafe, ''),
        ('EXPLAIN (TYPE LOGICAL) EXECUTE v', 'v=' + quote_plus(unsafe)),
    ]:
        with pytest.raises(HTTPException):
            sql_access.validate_statement(statement, prepared)
    sql_access.validate_statement('CREATE VIEW lake.reporting.v SECURITY INVOKER AS SELECT 1')
    sql_access.validate_statement("SELECT 'CREATE VIEW v SECURITY DEFINER' as value")
    sql_access.validate_statement('PREPARE v FROM SELECT 1')
    sql_access.validate_statement("EXECUTE IMMEDIATE 'SELECT ?' USING 3")


def test_schema_must_be_ready_and_system_metadata_is_bounded(sql_mode):
    client, headers = sql_mode
    with SessionLocal() as db:
        who = sql_access.actor(db, headers['Authorization'])
        for item in db.scalars(select(Resource).where(Resource.kind == 'schema')):
            item.config = {'state': 'pending'}
        db.commit()
        assert sql_access.scopes(db, who) == {}
    query = SqlQuery(scope={'finance': {'schemas': {'reporting': 'Reader'}, 'blocked_relations': []}})
    expression = sql_access.metadata_filters(query, {'catalogName': 'system', 'schemaName': 'metadata',
                                                    'tableName': 'table_comments'})[0]['expression']
    assert "catalog_name='finance'" in expression and "schema_name IN ('reporting')" in expression
    assert not sql_access.policy_allow(query, {'operation': 'SelectFromColumns', 'resource': {'table': {
        'catalogName': 'system', 'schemaName': 'runtime', 'tableName': 'queries'}}})


def test_engine_outage_does_not_leave_an_active_admission(sql_mode, monkeypatch):
    from fastapi import HTTPException
    client, headers = sql_mode
    async def unavailable(*args, **kwargs):
        raise HTTPException(503, 'SQL engine is unavailable')
    monkeypatch.setattr(sql_access, 'engine_request', unavailable)
    response = client.post('/v1/statement', headers=headers, content='SELECT 1')
    assert response.status_code == 503
    with SessionLocal() as db:
        assert db.scalar(select(SqlQuery)).state == 'FAILED'


def test_interactive_timeout_cancels_the_engine_request(sql_mode, monkeypatch):
    import asyncio
    client, headers = sql_mode
    calls = []
    async def engine(method, path, **kwargs):
        calls.append(method)
        if method == 'GET':
            raise asyncio.TimeoutError()
        if method == 'DELETE':
            return httpx.Response(204)
        return httpx.Response(200, json={'id': 'test_query', 'stats': {'state': 'QUEUED'},
            'nextUri': 'http://coordinator:8080/v1/statement/queued/test_query/token/1'})
    monkeypatch.setattr(sql_access, 'engine_request', engine)
    response = client.post('/v1/sql/query', headers=headers, json={'source': 'finance', 'sql': 'SELECT 1'})
    assert response.status_code == 504, response.text
    assert calls == ['POST', 'GET', 'DELETE']


def test_batch_scope_matches_additive_parent_and_schema_grants(sql_mode):
    client, headers = sql_mode
    with SessionLocal() as db:
        source = db.scalar(select(DataSource).where(DataSource.name == 'finance'))
        parent_id = rbac.source_resource(db, source).id
        child_id = db.scalar(select(Resource.id).where(Resource.object_id == source.id, Resource.kind == 'schema'))
    grant_group(client, parent_id, 'Source writers', 'Contributor', users=('ana',))
    grant_group(client, child_id, 'Schema administrators', 'Admin', users=('ana',))
    with SessionLocal() as db:
        who = sql_access.actor(db, headers['Authorization'])
        scope = sql_access.scopes(db, who)
        for catalog, source in sql_access.catalog_sources(db).items():
            assert scope[catalog]['schemas'] == rbac.data_scope(db, who.user, source)
        assert scope['finance']['schemas']['reporting'] == 'Admin'
        assert scope['lake']['schemas']['reporting'] == 'Reader'


def test_jdbc_metadata_is_readable_but_never_writable():
    # Trino lists system.jdbc rows through the Filter* callbacks, so reading them is safe.
    query = SqlQuery(scope={'finance': {'schemas': {'reporting': 'Reader'}}})
    jdbc = {'catalogName': 'system', 'schemaName': 'jdbc', 'tableName': 'tables'}
    assert sql_access.policy_allow(query, {'operation': 'SelectFromColumns', 'resource': {'table': jdbc}})
    assert not sql_access.policy_allow(query, {'operation': 'InsertIntoTable', 'resource': {'table': jdbc}})
    assert not sql_access.policy_allow(query, {'operation': 'SelectFromColumns', 'resource': {'table': {
        'catalogName': 'system', 'schemaName': 'runtime', 'tableName': 'queries'}}})
