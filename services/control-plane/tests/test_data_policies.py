import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import data_policies, rbac, sql_access
from app.auth import User, mint_token
from app.config import get_settings
from app.db import SessionLocal
from app.models import AuditEvent, DataPolicy, DataSource, Resource, SqlQuery, Workspace
from .conftest import as_
from .rbac_helpers import grant_group, integration


@pytest.mark.parametrize('expression', [
    "x IN (SELECT a FROM other)", "1; DROP TABLE x", "a, b", "region AS r", "a FROM t", "is_member(region)",
    "is_member('a', 'b')", "", "EXISTS (SELECT 1)",
])
def test_expressions_are_single_and_local(expression):
    with pytest.raises(HTTPException):
        data_policies.normalize_expression(expression)


def test_expressions_are_stored_normalized_without_comments():
    assert data_policies.normalize_expression("region = 'EU' -- ) OR TRUE") == "region = 'EU'"
    assert data_policies.member_groups("is_member('HR') OR is_member('Finance') AND x > 1") == ['Finance', 'HR']


def test_is_member_resolves_per_caller():
    expression = "region = 'EU' OR is_member('Global analysts')"
    assert data_policies.render(expression, {'global analysts'}) == "region = 'EU' OR TRUE"
    assert data_policies.render(expression, set()) == "region = 'EU' OR FALSE"


@pytest.mark.parametrize('method,column_type,expected', [
    ('null', 'integer', 'CAST(NULL AS integer)'),
    ('redact', 'varchar(64)', "CAST('****' AS varchar(64))"),
    ('redact', 'date', 'CAST(NULL AS date)'),
    ('hash', 'varchar', 'CAST(to_hex(sha256(to_utf8("email"))) AS varchar)'),
    ('hash', 'bigint', 'CAST(NULL AS bigint)'),
    ('partial', 'char(16)', "WHEN length(\"email\") <= 4 THEN rpad('', length(\"email\"), '*')"),
    ('partial', 'varchar', "concat(rpad('', length(\"email\") - 4, '*'), substr(\"email\", length(\"email\") - 3))"),
    ('null', 'varchar); DROP TABLE x; --', 'CAST(NULL AS varchar)'),
])
def test_mask_sql(method, column_type, expected):
    assert expected in data_policies.mask_sql(method, '', 'email', column_type, [])


def test_expression_mask_resolves_membership_and_keeps_the_column_type():
    sql = data_policies.mask_sql('expression', "CASE WHEN is_member('HR') THEN salary END", 'salary', 'decimal(10,2)', ['HR'])
    assert sql == 'CAST((CASE WHEN TRUE THEN salary END) AS decimal(10,2))'


@pytest.fixture
def governed(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, 'query_executor', 'trino')
    monkeypatch.setattr(s, 'trino_signing_key', 'dev-only-trino-key-change-me-01234567890123456789')
    monkeypatch.setattr(sql_access, 'check_native_relations', lambda query: None)
    client.put('/v1/admin/datasources/finance', headers=as_('admin'), json=integration('finance', 'postgres')).raise_for_status()
    schema = client.post('/v1/rbac/sources/finance/schemas', headers=as_('admin'), json={'name': 'reporting'}).json()
    readers = grant_group(client, schema['id'], 'finance-readers', 'Reader', users=('ana', 'bob'))
    auditors = grant_group(client, schema['id'], 'finance-auditors', 'Reader', users=('bob',))
    with SessionLocal() as db:
        resource = db.get(Resource, schema['id'])
        resource.config = {**resource.config, 'state': 'ready'}
        db.commit()
    return client, schema['id'], readers, auditors


def policy(client, schema_id, user='admin', **body):
    return client.post('/v1/data-policies', headers=as_(user), json={'schema_id': schema_id, **body})


def test_owners_manage_policies_and_readers_see_them(governed, monkeypatch):
    from app import auth
    from .conftest import USERS
    monkeypatch.setitem(USERS, 'carol', auth.User('carol'))  # no access to the schema
    client, schema_id, readers, auditors = governed
    body = {'table': 'Customers', 'kind': 'column_mask', 'column': 'email', 'method': 'partial', 'exempt_groups': [auditors]}
    assert policy(client, schema_id, 'ana', **body).status_code == 403
    created = policy(client, schema_id, **body)
    assert created.status_code == 201, created.text
    assert created.json()['table'] == 'customers'
    assert policy(client, schema_id, **body).status_code == 409
    assert [p['id'] for p in client.get('/v1/data-policies', headers=as_('ana')).json()] == [created.json()['id']]
    assert client.get('/v1/data-policies', headers=as_('carol')).json() == []
    assert policy(client, schema_id, table='customers', kind='row_filter', expression="is_member('nobody')").status_code == 422
    assert policy(client, schema_id, table='customers', kind='column_mask', column='phone', method='null',
                  exempt_groups=['missing']).status_code == 422
    assert policy(client, schema_id, table='customers', kind='column_mask', column='phone', method='null',
                  expression='1').status_code == 422
    changed = client.put('/v1/data-policies/' + created.json()['id'], headers=as_('admin'),
                         json={'method': 'hash', 'exempt_groups': []})
    assert changed.status_code == 200 and changed.json()['method'] == 'hash' and changed.json()['column'] == 'email'
    assert client.put('/v1/data-policies/' + created.json()['id'], headers=as_('ana'), json={'method': 'null'}).status_code == 403
    assert client.delete('/v1/data-policies/' + created.json()['id'], headers=as_('admin')).status_code == 204
    with SessionLocal() as db:
        actions = [e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.action.like('data_policy.%')))]
    assert actions == ['data_policy.create', 'data_policy.update', 'data_policy.delete']


def admitted(client, monkeypatch, user):
    sent = []

    async def engine(method, path, **kwargs):
        sent.append(kwargs)
        return httpx.Response(200, json={'id': 'q', 'stats': {'state': 'RUNNING'},
                                         'nextUri': 'http://coordinator:8080/v1/statement/executing/q/t/1'})
    monkeypatch.setattr(sql_access, 'engine_request', engine)
    with SessionLocal() as db:
        who = rbac.resolve_user(db, User(user))
        ws = Workspace(name='SQL ' + user, owner=user, principal_id=who.principal_id, editor='sql', status='running')
        db.add(ws)
        db.commit()
        token = mint_token('execution', user, 3600, ws=ws.id, pid=who.principal_id)
    response = client.post('/v1/statement', headers={'Authorization': 'Bearer ' + token, 'X-Trino-Catalog': 'finance'},
                           content='SELECT * FROM finance.reporting.customers')
    assert response.status_code == 200, response.text
    context = sent[0]['headers']['X-Trino-Extra-Credential'].split('=', 1)[1]
    with SessionLocal() as db:
        query = db.scalars(select(SqlQuery).order_by(SqlQuery.created_at.desc())).first()
        query.state = 'RUNNING'
        db.commit()
        return {'context': {'identity': {'user': query.principal_id, 'extraCredentials': {'dc_context': context}}}}


def columns(*names):
    return [{'column': {'catalogName': 'finance', 'schemaName': 'reporting', 'tableName': 'customers',
                        'columnName': name, 'columnType': 'varchar'}} for name in names]


def test_trino_receives_masks_and_filters_for_the_caller(governed, monkeypatch):
    client, schema_id, readers, auditors = governed
    assert policy(client, schema_id, table='customers', kind='column_mask', column='email', method='redact',
                  exempt_groups=[auditors]).status_code == 201
    assert policy(client, schema_id, table='customers', kind='row_filter',
                  expression="region = 'EU' OR is_member('finance-auditors')").status_code == 201
    table = {'catalogName': 'finance', 'schemaName': 'reporting', 'tableName': 'customers'}

    ana = admitted(client, monkeypatch, 'ana')
    masks = client.post('/internal/trino/batch-column-masks', json={'input': {**ana, 'action': {
        'operation': 'GetColumnMask', 'filterResources': columns('id', 'email')}}}).json()
    assert masks == {'result': [{'index': 1, 'viewExpression': {'expression': "CAST('****' AS varchar)"}}]}
    filters = client.post('/internal/trino/row-filters', json={'input': {**ana, 'action': {
        'operation': 'GetRowFilters', 'resource': {'table': table}}}}).json()
    assert filters == {'result': [{'expression': "region = 'EU' OR FALSE"}]}
    # Iceberg metadata tables would expose bounds and partition values of the protected table.
    files = {'input': {**ana, 'action': {'operation': 'SelectFromColumns', 'resource': {'table': {**table, 'tableName': 'customers$files'}}}}}
    assert client.post('/internal/trino/allow', json=files).json() == {'result': False}
    plain = {'input': {**ana, 'action': {'operation': 'SelectFromColumns', 'resource': {'table': table}}}}
    assert client.post('/internal/trino/allow', json=plain).json() == {'result': True}

    bob = admitted(client, monkeypatch, 'bob')  # an auditor: exempt from the mask, all rows through is_member
    assert client.post('/internal/trino/batch-column-masks', json={'input': {**bob, 'action': {
        'operation': 'GetColumnMask', 'filterResources': columns('id', 'email')}}}).json() == {'result': []}
    assert client.post('/internal/trino/row-filters', json={'input': {**bob, 'action': {
        'operation': 'GetRowFilters', 'resource': {'table': table}}}}).json() == {'result': [{'expression': "region = 'EU' OR TRUE"}]}


def test_mask_callback_fails_closed_without_a_valid_context(governed):
    client, *_ = governed
    response = client.post('/internal/trino/batch-column-masks', json={'input': {'context': {'identity': {
        'user': 'x', 'extraCredentials': {'dc_context': 'forged'}}}, 'action': {'filterResources': columns('email')}}})
    assert response.status_code != 200


def test_native_logins_are_refused_for_schemas_with_policies(governed):
    client, schema_id, *_ = governed
    from app.data_access import credentials
    assert policy(client, schema_id, table='customers', kind='column_mask', column='email', method='null').status_code == 201
    with SessionLocal() as db:
        source = db.scalar(select(DataSource).where(DataSource.name == 'finance'))
        with pytest.raises(HTTPException) as refused:
            credentials(db, source, {'reporting': 'Reader'}, None, 'ana')
    assert refused.value.status_code == 409 and 'governed SQL' in refused.value.detail


def test_policies_follow_their_schema(governed):
    client, schema_id, *_ = governed
    assert policy(client, schema_id, table='customers', kind='column_mask', column='email', method='null').status_code == 201
    with SessionLocal() as db:
        assert db.scalar(select(DataPolicy.schema_id)) == schema_id


def test_normalize_endpoint(governed):
    client, *_ = governed
    response = client.post('/v1/data-policies/normalize', headers=as_('ana'), json={'expression': "region='EU' -- x"})
    assert response.json() == {'expression': "region = 'EU'", 'member_groups': []}
    assert client.post('/v1/data-policies/normalize', headers=as_('ana'), json={'expression': '(SELECT 1)'}).status_code == 422


def test_statistics_are_refused_while_policies_apply(governed, monkeypatch):
    client, schema_id, readers, auditors = governed
    assert policy(client, schema_id, table='customers', kind='row_filter', expression="region = 'EU'",
                  exempt_groups=[auditors]).status_code == 201
    admitted(client, monkeypatch, 'bob')  # exempt: nothing applies, so statistics stay available
    with SessionLocal() as db:
        exempt = db.scalars(select(SqlQuery).order_by(SqlQuery.created_at.desc())).first()
        sql_access.authorize_initial_creates(exempt, 'SHOW STATS FOR finance.reporting.customers')
    admitted(client, monkeypatch, 'ana')
    with SessionLocal() as db:
        query = db.scalars(select(SqlQuery).order_by(SqlQuery.created_at.desc())).first()
        for text in ('SHOW STATS FOR finance.reporting.customers', 'show  stats for (SELECT * FROM customers)',
                     'EXPLAIN SHOW STATS FOR customers', "EXECUTE IMMEDIATE 'SHOW STATS FOR customers'"):
            with pytest.raises(HTTPException) as refused:
                sql_access.authorize_initial_creates(query, text)
            assert refused.value.status_code == 403, text
        sql_access.authorize_initial_creates(query, 'SELECT stats FROM customers')
