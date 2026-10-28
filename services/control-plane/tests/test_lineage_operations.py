"""Operational metadata must not bridge the default data lineage graph."""
from datetime import timedelta

import pytest

from app import catalog, catalog_ingest, rbac, sql_access
from app.auth import User, mint_token
from app.db import SessionLocal
from app.models import Resource, SqlQuery, utcnow
from .conftest import as_
from .test_catalog import register, asset
from .test_deploying import repo  # noqa: F401


def test_checkpoint_context_is_optional_scoped_and_preserved_when_focused(client, repo):
    _, loader = register(client, repo, kind='script', slug='load')
    _, dashboard = register(client, repo, kind='app', slug='dashboard')
    _, bootstrap = register(client, repo, kind='script', slug='bootstrap')
    _, unrelated = register(client, repo, kind='app', slug='other-consumer')
    asset(client, loader)
    with SessionLocal() as db:
        producer = db.get(Resource, loader)
        nodes = {name: catalog_ingest.dataset(db, producer, {'source':'finance-dw', 'schema':'public', 'name':name})
                 for name in ('records', 'load_commits', 'reporting')}
        nodes['load_commits'].governance = {'lineage_role':'operational'}
        model = catalog.upsert(db, producer, 'transform', kind='dbt_model', name='transform')
        for source, target, relation in [
            (catalog.upsert(db, producer), nodes['records'], 'writes'),
            (nodes['records'], model, 'reads'),
            (model, nodes['reporting'], 'writes'),
            (nodes['reporting'], catalog.upsert(db, db.get(Resource, dashboard)), 'reads'),
            (nodes['load_commits'], catalog.upsert(db, producer), 'reads'),
            (catalog.upsert(db, producer), nodes['load_commits'], 'writes'),
            (catalog.upsert(db, db.get(Resource, bootstrap)), nodes['records'], 'creates'),
            (nodes['load_commits'], catalog.upsert(db, db.get(Resource, unrelated)), 'reads'),
        ]:
            catalog.edge(db, producer, source, target, relation, 'trino_runtime', 'query')
        checkpoint = nodes['load_commits'].id
        db.commit()
    endpoint = f'/v1/catalog/assets/{catalog.asset_id(dashboard)}/lineage'
    params = {'view':'flow', 'direction':'both', 'depth':16}
    normal = client.get(endpoint, headers=as_('ana'), params=params).json()
    assert {n['name'] for n in normal['nodes']} == {'load','records','transform','reporting','dashboard'}
    expanded = client.get(endpoint, headers=as_('ana'), params={**params, 'operational':True}).json()
    assert {n['name'] for n in expanded['nodes']} == {'load','records','transform','reporting','dashboard','load_commits','bootstrap'}
    technical = [e for e in expanded['edges'] if e['category'] != 'data']
    assert len(technical) == 3 and all(e['control'] for e in technical)
    assert {e['category'] for e in technical} == {'operational','schema'}
    focused = client.get(f'/v1/catalog/assets/{checkpoint}/lineage', headers=as_('ana'), params=params).json()
    assert any(e['source'] == checkpoint and e['target'] == catalog.asset_id(loader) and e['control'] for e in focused['edges'])
    assert any(e['target'] == checkpoint and e['source'] == catalog.asset_id(loader) and e['control'] for e in focused['edges'])


@pytest.mark.parametrize('statement,expected', [
    ('CREATE TABLE public.new_table (id integer)', ['creates']),
    ('CREATE TABLE public.new_table AS SELECT 1 AS id', ['creates', 'writes']),
    ("EXECUTE IMMEDIATE 'CREATE TABLE public.new_table AS SELECT 1 AS id'", ['creates', 'writes']),
])
def test_create_callbacks_separate_empty_ddl_from_materialization(client, repo, statement, expected):
    pid, rid = register(client, repo, kind='script', slug='bootstrap')
    asset(client, rid)
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
        query = SqlQuery(principal_id=ana.principal_id, actor='ana', binding='test', workload_id=pid,
            scope={'finance_dw':{'schemas':{'public':'Owner'}}}, expires_at=utcnow()+timedelta(minutes=10))
        sql_access.authorize_initial_creates(query, statement, catalog='finance_dw', schema='public')
        db.add(query); db.commit()
        qid, principal = query.id, ana.principal_id
    context = {'identity':{'user':principal,'extraCredentials':{
        'dc_context':mint_token('trino-policy',principal,60,q=qid)}}}
    table = {'catalogName':'finance_dw','schemaName':'public','tableName':'new_table'}
    response = client.post('/internal/trino/allow', json={'input':{'context':context,'action':{
        'operation':'CreateTable','resource':{'table':table}}}})
    assert response.json() == {'result':True}
    with SessionLocal() as db:
        query = db.get(SqlQuery, qid)
        assert next(iter(query.lineage.values()))['relations'] == expected
        catalog.successful_query(db, query)
        db.commit()
    graph = client.get(f'/v1/catalog/assets/{catalog.asset_id(rid)}/lineage', headers=as_('ana')).json()
    edges = {e['relation']:e for e in graph['edges']}
    assert edges['creates']['category'] == 'schema'
    assert ('writes' in edges) == ('writes' in expected)
