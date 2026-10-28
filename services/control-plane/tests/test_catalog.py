"""Catalog discovery, governance authority, native lineage and measured SLA regressions."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import catalog, rbac, deps
from app.auth import User, mint_token
from app.db import SessionLocal
from app.models import Workload, Resource, CatalogAsset, Run, ComputePool, LineageEdge, CatalogImport, utcnow
from .conftest import as_
from .rbac_helpers import grant_group
from .test_deploying import repo, deploy  # noqa: F401


def register(client, repo, kind="dbt", slug="models"):
    with SessionLocal() as db:
        pub = Workload(repo_id=repo["id"], slug=slug, title=slug, branch="main", program=slug,
            kind=kind, runtime="python", owner="ana", warehouse="small", status="live", deployed_sha="a" * 40,
            program_def={"kind": kind, "description": "Finance product", "data_sources": ["finance-dw"],
                "dbt": {"data_source": "finance-dw", "schema": "public"}})
        db.add(pub); db.flush()
        resource = rbac.workload_resource(db, pub)
        db.commit()
        return pub.id, resource.id


def manifest():
    return {"sources": {"source.finance.sales": {"name": "sales", "identifier": "sales", "resource_type": "source", "database": "finance", "schema": "public"}},
        "nodes": {"model.finance.revenue": {"name": "revenue", "alias": "revenue", "resource_type": "model", "database": "finance", "schema": "public",
            "description": "Revenue after refunds", "config": {"materialized": "table"},
            "columns": {"amount": {"description": "Net revenue", "data_type": "decimal"}},
            "depends_on": {"nodes": ["source.finance.sales"]}, "compiled_code": "SECRET SQL MUST NOT APPEAR"}}}


def asset(client, resource_id):
    result = client.get('/v1/catalog/search', headers=as_('ana'))
    assert result.status_code == 200, result.text
    return next(a for a in result.json()['items'] if a['id'] == catalog.asset_id(resource_id))


def test_catalog_search_is_permission_filtered_ranked_and_never_crawls(client, repo, monkeypatch):
    _, rid = register(client, repo)
    # Neither search nor filters may call out to SQL engines or object storage.
    import app.routes.data_browser as browser
    monkeypatch.setattr(browser, 'storage_children', lambda *a, **k: (_ for _ in ()).throw(AssertionError('crawl')))
    a = asset(client, rid)
    profile = {'description': 'Financial sales metrics', 'domain': 'Finance', 'tags': ['monthly', 'revenue']}
    assert client.put(f"/v1/catalog/assets/{a['id']}/governance", headers=as_('ana'), json=profile).status_code == 200
    search = client.get('/v1/catalog/search', headers=as_('ana'), params={'q': 'finance "sales metrics" monthly', 'domain': 'Finance'})
    assert search.status_code == 200 and search.json()['total'] == 1
    assert search.json()['items'][0]['id'] == a['id']
    assert client.get('/v1/catalog/search?limit=1', headers=as_('ana')).json()['limit'] == 1
    for group in client.get('/v1/rbac/groups', headers=as_('admin')).json():
        if group['name'] == 'Deployed executors':
            client.delete(f"/v1/rbac/resources/workloads/assignments/{group['id']}", headers=as_('admin'))
    hidden = client.get('/v1/catalog/search?q=monthly', headers=as_('bob')).json()
    assert hidden['total'] == 0 and hidden['facets']['domain'] == []
    assert client.get(f"/v1/catalog/assets/{a['id']}", headers=as_('bob')).status_code == 404


def test_search_expressions_ranking_typos_columns_and_private_suggestions(client, repo):
    _, rid = register(client, repo)
    base = asset(client, rid)
    assert client.post(f'/v1/catalog/resources/{rid}/import/dbt', headers=as_('ana'), json={'manifest': manifest()}).status_code == 200
    gid = grant_group(client, rid, 'Revenue owners', 'Owner', users=['ana'])
    assert client.put(f"/v1/catalog/assets/{base['id']}/governance", headers=as_('ana'), json={
        'description': 'Monthly financial reporting', 'domain': 'Finance', 'owner_group_id': gid,
        'tags': ['monthly'], 'status': 'reviewed'}).status_code == 200

    def search(q, **params):
        response = client.get('/v1/catalog/search', headers=as_('ana'), params={'q': q, **params})
        assert response.status_code == 200, response.text
        return response.json()

    structured = search('domain:Finance (type:dbt OR type:table) -status:deprecated')
    assert [a['id'] for a in structured['items']] == [base['id']]
    assert search('name:rev* column:amount')['total'] >= 1
    assert search('"Revenue after refunds"')['total'] >= 1
    assert search('revenue')['items'][0]['name'] == 'revenue'
    fuzzy = search('reveneu')
    assert fuzzy['approximate'] and fuzzy['items'][0]['name'] == 'revenue'
    assert search('reveneu', fuzzy='false')['total'] == 0
    assert search('revenue NOT type:table')['total'] >= 1
    assert search('owner:"Revenue owners" tag:monthly')['total'] == 1
    # Owner-name matches remain visible even when another object's title hits the full-text index.
    assert base['id'] in {a['id'] for a in search('revenue')['items']}
    assert search('', column='amount')['total'] >= 1
    assert search('column:amount', platform='postgres')['total'] >= 1
    assert search('', tag='monthly', owner='Revenue owners')['total'] == 1
    assert search('refunds')['total'] >= 1
    assert 'kind' in search('type:dbt')['items'][0]['matched_fields']
    for invalid in ['"open quote', '(revenue', 'revenue OR', 'unknown:value', 'AND revenue']:
        assert client.get('/v1/catalog/search', headers=as_('ana'), params={'q': invalid}).status_code == 422
    suggestions = client.get('/v1/catalog/suggest?q=reveneu', headers=as_('ana')).json()['items']
    assert suggestions and suggestions[0]['approximate']
    for group in client.get('/v1/rbac/groups', headers=as_('admin')).json():
        if group['name'] == 'Deployed executors':
            client.delete(f"/v1/rbac/resources/workloads/assignments/{group['id']}", headers=as_('admin'))
    assert client.get('/v1/catalog/suggest?q=revenue', headers=as_('bob')).json()['items'] == []
    assert search('domain:Finance')['total'] == 1
    # Updating metadata updates the database-native full-text index without a rebuild.
    assert client.put(f"/v1/catalog/assets/{base['id']}/governance", headers=as_('ana'), json={'description': 'Café liquidity forecasting'}).status_code == 200
    assert search('cafe liquidity')['total'] == 1
    assert search('monthly')['total'] == 0


def test_lineage_neighbor_counts_expansion_and_observation_window_are_scoped(client, repo):
    _, rid = register(client, repo)
    base = asset(client, rid)
    assert client.post(f'/v1/catalog/resources/{rid}/import/dbt', headers=as_('ana'), json={'manifest': manifest()}).status_code == 200
    endpoint = f"/v1/catalog/assets/{base['id']}/lineage"
    first = client.get(endpoint, headers=as_('ana'), params={'depth': 1, 'direction': 'downstream'}).json()
    root = next(n for n in first['nodes'] if n['id'] == base['id'])
    assert root['downstream_count'] > 0
    assert len(first['nodes']) > 1
    with SessionLocal() as db:
        for edge in db.scalars(select(LineageEdge).where(LineageEdge.producer_id == rid)):
            edge.observed_at = utcnow() - timedelta(days=10)
        db.commit()
    recent = client.get(endpoint, headers=as_('ana'), params={'since': (utcnow() - timedelta(days=1)).isoformat()}).json()
    assert len(recent['nodes']) == 1 and recent['edges'] == []
    assert recent['nodes'][0]['upstream_count'] == recent['nodes'][0]['downstream_count'] == 0
    assert client.get(endpoint, headers=as_('ana'), params={'since': 'not-a-date'}).status_code == 422


def test_governance_and_sla_cannot_promote_contributors_or_change_iam(client, repo):
    _, rid = register(client, repo)
    a = asset(client, rid)
    gid = grant_group(client, rid, 'Product editors', 'Contributor', users=['bob'])
    path = f"/v1/catalog/assets/{a['id']}"
    doc = {'description': 'Monthly revenue', 'domain': 'Finance', 'tags': ['revenue']}
    assert client.put(path + '/governance', headers=as_('bob'), json=doc).status_code == 200
    for key, value in [('status', 'certified'), ('classification', 'public'), ('owner_group_id', gid), ('steward', 'bob'), ('lineage_role', 'operational')]:
        assert client.put(path + '/governance', headers=as_('bob'), json={**doc, key: value}).status_code == 403
    assert client.put(path + '/sla', headers=as_('bob'), json={'freshness_minutes': 60}).status_code == 403
    certified = {**doc, 'owner_group_id': gid, 'steward': 'finance@example.test', 'classification': 'internal', 'status': 'certified'}
    assert client.put(path + '/governance', headers=as_('ana'), json={**certified, 'steward': ''}).status_code == 422
    assert client.put(path + '/governance', headers=as_('ana'), json=certified).json()['governance']['status'] == 'certified'
    edited = client.put(path + '/governance', headers=as_('bob'), json={**certified, 'description': 'Updated revenue'})
    assert edited.status_code == 200 and edited.json()['governance']['status'] == 'reviewed'
    grants = client.get(f'/v1/rbac/resources/{rid}/assignments', headers=as_('ana')).json()
    assert next(g for g in grants if g['app_group_id'] == gid)['role'] == 'Contributor'


def test_dbt_native_lineage_is_idempotent_and_has_columns_without_sql(client, repo):
    _, rid = register(client, repo)
    base = asset(client, rid)
    now = utcnow() - timedelta(minutes=10)
    # Attesting another dataset's refresh requires data-write authority, beyond
    # permission to import the project's descriptive lineage.
    with SessionLocal() as db:
        from app.models import ServiceRoleAssignment, DataSource
        ana=rbac.resolve_user(db,User('ana'))
        source=db.scalar(select(DataSource).where(DataSource.name=='finance-dw'))
        schema=db.scalar(select(Resource).where(Resource.kind=='schema',Resource.object_id==source.id,Resource.name=='public'))
        db.add(ServiceRoleAssignment(resource_id=schema.id,principal_id=ana.principal_id,role='Contributor'))
        db.commit()
    payload = {'manifest': manifest(), 'catalog': {'nodes': {'model.finance.revenue': {'columns': {'amount': {'type': 'decimal(12,2)'}}}}},
        'run_results': {'results': [{'unique_id': 'model.finance.revenue', 'status': 'success', 'execution_time': 2,
                                    'timing': [{'name': 'execute', 'completed_at': now.isoformat()}]}]}}
    url = f'/v1/catalog/resources/{rid}/import/dbt'
    for _ in range(2):
        response = client.post(url, headers=as_('ana'), json=payload)
        assert response.status_code == 200, response.text
    graph = client.get(f"/v1/catalog/assets/{base['id']}/lineage?depth=4", headers=as_('ana'))
    assert graph.status_code == 200 and 'SECRET SQL' not in graph.text
    nodes = graph.json()['nodes']
    revenue = next(n for n in nodes if n['kind'] == 'table' and n['name'] == 'revenue')
    assert revenue['attributes']['columns'][0]['data_type'] == 'decimal(12,2)'
    assert any(e['relation'] == 'writes' and e['target'] == revenue['id'] for e in graph.json()['edges'])
    assert client.put(f"/v1/catalog/assets/{revenue['id']}/sla", headers=as_('admin'), json={'freshness_minutes': 60}).json()['service_level']['status'] == 'healthy'
    with SessionLocal() as db:
        assert len(list(db.scalars(select(CatalogImport).where(CatalogImport.resource_id == rid)))) == 1
    # Removing a dependency must remove the old edge on the next snapshot.
    payload['manifest']['nodes']['model.finance.revenue']['depends_on']['nodes'] = []
    assert client.post(url, headers=as_('ana'), json=payload).status_code == 200
    with SessionLocal() as db:
        assert not any(e.relation == 'depends_on' for e in db.scalars(select(LineageEdge).where(LineageEdge.producer_id == rid)))


def test_lineage_never_bridges_a_hidden_producer_or_endpoint(client, repo):
    _, rid = register(client, repo)
    a = asset(client, rid)
    assert client.post(f'/v1/catalog/resources/{rid}/import/dbt', headers=as_('ana'), json={'manifest': manifest()}).status_code == 200
    graph = client.get(f"/v1/catalog/assets/{a['id']}/lineage?depth=5", headers=as_('bob')).json()
    assert all(n['resource']['kind'] != 'schema' for n in graph['nodes'])
    assert all(e['source'] in {n['id'] for n in graph['nodes']} and e['target'] in {n['id'] for n in graph['nodes']} for e in graph['edges'])
    for group in client.get('/v1/rbac/groups', headers=as_('admin')).json():
        if group['name'] == 'Deployed executors':
            client.delete(f"/v1/rbac/resources/workloads/assignments/{group['id']}", headers=as_('admin'))
    # Data access alone must not reveal a hidden dbt producer through graph connectivity.
    with SessionLocal() as db:
        schema = db.scalar(select(Resource).where(Resource.kind == 'schema'))
        schema_id = schema.id
    grant_group(client, schema_id, 'Bob data readers', 'Reader', users=['bob'])
    tables = client.get('/v1/catalog/search?kind=table', headers=as_('bob')).json()['items']
    assert tables
    graph = client.get(f"/v1/catalog/assets/{tables[0]['id']}/lineage?depth=5", headers=as_('bob')).json()
    assert graph['edges'] == [] and len(graph['nodes']) == 1


def test_airflow_native_dags_preserve_each_snapshot_and_link_deployed_programs(client, repo):
    _, airflow_id = register(client, repo, kind='airflow', slug='pipelines')
    _, models_id = register(client, repo, slug='models')
    base = asset(client, airflow_id)
    endpoint = f'/v1/catalog/resources/{airflow_id}/import/airflow'
    first = {'dag_id': 'daily', 'tasks': [{'task_id': 'extract', 'outlets': [{'uri': 'fdp://finance-dw/public/sales'}]},
        {'task_id': 'models', 'upstream_task_ids': ['extract'], 'workload': 'models', 'inlets': [{'source': 'finance-dw', 'schema': 'public', 'name': 'sales'}]}]}
    assert client.post(endpoint, headers=as_('bob'), json=first).status_code == 403
    for payload in [first, {'dag_id': 'weekly', 'tasks': [{'task_id': 'summarize', 'program': 'sales-report'}]}, first]:
        response = client.post(endpoint, headers=as_('ana'), json=payload)
        assert response.status_code == 200, response.text
    graph = client.get(f"/v1/catalog/assets/{base['id']}/lineage?depth=5", headers=as_('ana')).json()
    assert {n['name'] for n in graph['nodes']} >= {'daily', 'weekly', 'extract', 'models', 'sales-report'}
    assert any(e['relation'] == 'orchestrates' and e['target'] == catalog.asset_id(models_id) for e in graph['edges'])
    assert any(e['relation'] == 'depends_on' and e['origin'] == 'airflow_dag' for e in graph['edges'])
    # Signed external URIs are ignored rather than catalogued or fetched.
    unsafe = {'dag_id': 'unsafe', 'tasks': [{'task_id': 'no-fetch', 'inlets': [{'uri': 'https://secret:password@evil.test/?token=secret'}]}]}
    assert client.post(endpoint, headers=as_('ana'), json=unsafe).status_code == 200
    assert 'password' not in client.get('/v1/catalog/search', headers=as_('ana')).text


def test_slas_use_completed_runs_exclude_docs_and_show_unknown(client, repo):
    pid, rid = register(client, repo)
    a = asset(client, rid)
    endpoint = f"/v1/catalog/assets/{a['id']}/sla"
    response = client.put(endpoint, headers=as_('ana'), json={'freshness_minutes': 60, 'success_rate_percent': 90, 'window_hours': 24})
    assert response.json()['service_level']['status'] == 'unknown'
    with SessionLocal() as db:
        pool = db.scalar(select(ComputePool).where(ComputePool.name == 'small'))
        for state, command, age in [('succeeded', 'build', 120), ('failed', 'build', 20), ('succeeded', 'docs', 1)]:
            db.add(Run(owner='ana', workload_id=pid, pool_id=pool.id, kind='dbt', entrypoint='dbt', program='models', status=state,
                       args={'command': command}, finished_at=utcnow() - timedelta(minutes=age)))
        db.commit()
    state = client.get(f"/v1/catalog/assets/{a['id']}", headers=as_('ana')).json()['service_level']
    assert state['status'] == 'breached'
    assert state['checks'][0]['value'] >= 120 and state['checks'][1]['value'] == 50
    assert state['checks'][1]['samples'] == 2


def test_ingestion_is_bound_to_running_workloads_and_not_oidc_tokens(client, repo):
    pid, rid = register(client, repo)
    asset(client, rid)
    payload = {'manifest': manifest()}
    assert client.post('/v1/catalog/ingest/dbt', headers=as_('ana'), json=payload).status_code == 401
    token = mint_token('execution', 'service:other', 60, pub=pid, sha='a' * 40)
    assert client.post('/v1/catalog/ingest/dbt', headers={'Authorization': 'Bearer ' + token}, json=payload).status_code == 401
    with SessionLocal() as db:
        pub = db.get(Workload, pid)
        token = mint_token('execution', pub.identity, 60, pub=pid, sha='a' * 40)
    response = client.post('/v1/catalog/ingest/dbt', headers={'Authorization': 'Bearer ' + token}, json=payload)
    assert response.status_code == 200, response.text


def test_trino_callbacks_record_only_permitted_tables_and_completed_queries(client, repo):
    import httpx
    from starlette.requests import Request
    from app.models import SqlQuery
    from app.routes.sql import finish_response
    pid, rid = register(client, repo, kind='script', slug='report')
    base = asset(client, rid)
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
        query = SqlQuery(principal_id=ana.principal_id, actor='ana', binding='test', workload_id=pid,
            scope={'finance_dw': {'schemas': {'public': 'Reader'}}}, expires_at=utcnow() + timedelta(minutes=10))
        db.add(query); db.commit()
        qid = query.id
        context = {'identity': {'user': ana.principal_id, 'extraCredentials': {'dc_context': mint_token('trino-policy', ana.principal_id, 60, q=qid)}}}
    table = {'catalogName': 'finance_dw', 'schemaName': 'public', 'tableName': 'sales'}
    for operation, allowed in [('SelectFromColumns', True), ('InsertIntoTable', False)]:
        response = client.post('/internal/trino/allow', json={'input': {'context': context, 'action': {'operation': operation, 'resource': {'table': table}}}})
        assert response.json() == {'result': allowed}
    with SessionLocal() as db:
        query = db.get(SqlQuery, qid)
        assert next(iter(query.lineage.values()))['relations'] == ['reads']
        assert not list(db.scalars(select(LineageEdge).where(LineageEdge.origin == 'trino_runtime')))
        finish_response(db, query, httpx.Response(200, json={'id': 'native', 'stats': {'state': 'FINISHED'}}),
            Request({'type': 'http', 'scheme': 'http', 'server': ('testserver', 80), 'path': '/', 'headers': []}))
    graph = client.get(f"/v1/catalog/assets/{base['id']}/lineage", headers=as_('ana')).json()
    assert any(e['origin'] == 'trino_runtime' and e['relation'] == 'reads' for e in graph['edges'])
    assert not any(e['origin'] == 'trino_runtime' and e['relation'] == 'writes' for e in graph['edges'])


def test_concurrent_program_reports_preserve_successful_output_observations(client, repo):
    pid, rid = register(client, repo, kind='script', slug='refresh')
    asset(client, rid)
    ids, tokens = [], []
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
        pool = db.scalar(select(ComputePool).where(ComputePool.name == 'small'))
        from app.models import DataSource
        source=db.scalar(select(DataSource).where(DataSource.name=='finance-dw'))
        for _ in range(2):
            run = Run(owner='ana', principal_id=ana.principal_id, workload_id=pid, pool_id=pool.id,
                      kind='script', program='refresh', entrypoint='refresh.py', status='running',access_snapshot={source.id:{'public':'Contributor'}})
            db.add(run); db.flush()
            ids.append(run.id)
            tokens.append(mint_token('execution', 'ana', 60, pub=pid, run=run.id, pid=ana.principal_id))
        db.commit()
    for token in tokens:
        response = client.post('/v1/catalog/ingest/program', headers={'Authorization': 'Bearer ' + token},
            json={'outputs': [{'source': 'finance-dw', 'schema': 'public', 'name': 'daily_refresh'}]})
        assert response.status_code == 200, response.text
    with SessionLocal() as db:
        output = db.scalar(select(CatalogAsset).where(CatalogAsset.name == 'daily_refresh'))
        assert output.last_success_at is None
        run = db.get(Run, ids[0]); run.status, run.finished_at = 'succeeded', utcnow()
        catalog.finished_run(db, run)
        db.commit()
        assert output.last_success_at is not None


def test_completed_dbt_artifacts_backfill_without_a_runtime_sdk(client, repo, monkeypatch, tmp_path):
    import json
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'data_dir', str(tmp_path))
    pid, rid = register(client, repo)
    with SessionLocal() as db:
        pool = db.scalar(select(ComputePool).where(ComputePool.name == 'small'))
        run = Run(owner='ana', workload_id=pid, pool_id=pool.id, kind='dbt', program='models',
                  entrypoint='dbt', status='succeeded', finished_at=utcnow())
        db.add(run); db.commit()
        root = tmp_path / 'runs' / run.id / 'outputs'
        root.mkdir(parents=True)
        (root / 'manifest.json').write_text(json.dumps(manifest()))
    response = client.get('/v1/catalog/search?q=revenue', headers=as_('ana'))
    assert response.status_code == 200 and response.json()['total'] >= 1
    assert any(a['kind'] == 'dbt_model' for a in response.json()['items'])


def test_malformed_native_artifacts_are_validation_errors(client, repo):
    _, rid = register(client, repo)
    for body in [{'manifest': {'nodes': []}}, {'manifest': {'nodes': {'model.x': 'invalid'}}},
                 {'manifest': manifest(), 'run_results': {'results': 'invalid'}}]:
        assert client.post(f'/v1/catalog/resources/{rid}/import/dbt', headers=as_('ana'), json=body).status_code == 422


def test_dbt_invoked_inside_airflow_uses_the_pinned_program_and_preserves_airflow_identity(client, repo):
    pid, rid = register(client, repo, kind='airflow', slug='pipelines')
    pulled = client.post(f"/v1/repos/{repo['id']}/pull?branch=main", headers=as_('ana')).json()
    sha = pulled['commit']['sha']
    with SessionLocal() as db:
        pub = db.get(Workload, pid); pub.deployed_sha = sha
        ana = rbac.resolve_user(db, User('ana'))
        pool = db.scalar(select(ComputePool).where(ComputePool.name == 'small'))
        run = Run(owner='ana', principal_id=ana.principal_id, workload_id=pid, pool_id=pool.id,
            program='finance-models', kind='dbt', entrypoint='dbt/finance', status='running', commit_sha=sha)
        db.add(run); db.flush()
        token = mint_token('execution', 'ana', 60, pub=pid, run=run.id, pid=ana.principal_id, sha=sha)
        db.commit()
    base = asset(client, rid)
    response = client.post('/v1/catalog/ingest/dbt', headers={'Authorization': 'Bearer ' + token}, json={'manifest': manifest()})
    assert response.status_code == 200, response.text
    graph = client.get(f"/v1/catalog/assets/{base['id']}/lineage?depth=5", headers=as_('ana')).json()
    assert next(n for n in graph['nodes'] if n['id'] == base['id'])['kind'] == 'airflow'
    assert any(n['name'] == 'finance-models' and n['kind'] == 'program' for n in graph['nodes'])
    assert any(n['kind'] == 'dbt_model' for n in graph['nodes'])


def test_empty_catalog_does_not_index_system_navigation_roots(client):
    resources = client.get('/v1/rbac/resources', headers=as_('admin'))
    assert resources.status_code == 200
    assert {r['kind'] for r in resources.json()} == {'root', 'api-root', 'package-root'}
    catalog = client.get('/v1/catalog/search', headers=as_('admin'))
    assert catalog.status_code == 200
    assert catalog.json()['items'] == [] and catalog.json()['total'] == 0




@pytest.mark.parametrize('declared', [True, False])
def test_focused_upstream_is_end_to_end_data_flow_without_project_or_test_spokes(client, repo, declared):
    from app.models import ApiDefinition, AuditEvent, SqlQuery
    model_id,model_resource=register(client,repo)
    app_id,app_resource=register(client,repo,kind='app',slug='dashboard')
    ingest_id,ingest_resource=register(client,repo,kind='script',slug='ingestion')
    external=client.post('/v1/apis',headers=as_('admin'),json={'name':'TCG API','slug':'tcg-api','direction':'external','outbound':{'base_url':'https://example.com'}}).json()
    grant_group(client,external['resource_id'],'Finance data','Consumer',users=['ana'])
    with SessionLocal() as db:
        pub=db.get(Workload,app_id)
        pub.program_def={'lineage':{'inputs':[{'source':'finance-dw','schema':'public','name':'current'}]}} if declared else {}
        pub=db.get(Workload,ingest_id)
        pub.program_def={'lineage':{'outputs':[{'source':'finance-dw','schema':'public','name':'sales'}]}} if declared else {}
        api=db.get(ApiDefinition,external['id'])
        db.add(AuditEvent(actor='service:'+pub.service_principal_id,action='api.egress',target=api.id,detail={'operation':'cards','status':200}))
        db.commit()
    doc=manifest()
    doc['nodes']['model.finance.current']={**doc['nodes']['model.finance.revenue'],'name':'current','alias':'current','depends_on':{'nodes':['model.finance.revenue']}}
    doc['nodes']['model.finance.sibling']={**doc['nodes']['model.finance.revenue'],'name':'sibling','alias':'sibling'}
    doc['nodes']['test.finance.current']={'name':'current_unique','resource_type':'test','depends_on':{'nodes':['model.finance.current']}}
    assert client.post(f'/v1/catalog/resources/{model_resource}/import/dbt',headers=as_('ana'),json={'manifest':doc}).status_code==200
    with SessionLocal() as db:
        catalog.sync_registry(db);catalog.harvest_api_calls(db)
        current=db.scalar(select(CatalogAsset).where(CatalogAsset.kind=='table',CatalogAsset.name=='current'))
        ana=rbac.resolve_user(db,User('ana'))
        # Successful engine observations connect ingestion and the dashboard without JSON inputs/outputs.
        for workload,table,relation in ((app_id,'current','reads'),(ingest_id,'sales','writes')):
            query=SqlQuery(principal_id=ana.principal_id,actor='ana',binding='test',workload_id=workload,
                state='FINISHED',expires_at=utcnow()+timedelta(minutes=10),lineage={table:{
                    'catalog':'finance_dw','schema':'public','table':table,'relations':[relation]}})
            db.add(query);db.flush()
            catalog.successful_query(db,query)
        # Runtime dbt project reads/writes must not form a shortcut around its real models.
        catalog.edge(db,db.get(Resource,model_resource),catalog.upsert(db,db.get(Resource,model_resource),kind='dbt'),current,'writes','trino_runtime','query-2')
        db.commit()
    endpoint=f'/v1/catalog/assets/{catalog.asset_id(app_resource)}/lineage'
    flow=client.get(endpoint,headers=as_('ana'),params={'view':'flow','direction':'upstream','depth':16}).json()
    assert {n['kind'] for n in flow['nodes']}=={'app','table','dbt_model','script','api'}
    assert not any(n['name'] in ('sibling','current_unique','models') for n in flow['nodes'])
    assert all(n['distance']<=0 for n in flow['nodes'])
    app_reads=[e for e in flow['edges'] if e['target']==catalog.asset_id(app_resource)]
    assert len(app_reads)==1 and {e['origin'] for e in app_reads[0]['evidence']}==({'program_declared','trino_runtime'} if declared else {'trino_runtime'})
    if not declared:
        assert not any(e['origin']=='program_declared' for e in flow['edges'])
    by_id={n['id']:n for n in flow['nodes']}
    assert any(e['origin']=='api_runtime' and by_id[e['source']]['kind']=='api' and by_id[e['target']]['kind']=='script' for e in flow['edges'])
    assert sum(n['kind']=='dbt_model' for n in flow['nodes'])==2
    full=client.get(endpoint,headers=as_('ana'),params={'view':'full','direction':'both','depth':16}).json()
    assert not any(n['kind']=='dbt_test' for n in full['nodes'])
    assert not any(n['name']=='sibling' for n in full['nodes'])
    # Visibility is applied before projection; hiding the API cannot be bridged by audit evidence.
    with SessionLocal() as db:
        from app.models import RoleAssignment
        for assignment in db.scalars(select(RoleAssignment).where(RoleAssignment.resource_id==external['resource_id'])):
            db.delete(assignment)
        db.commit()
    hidden=client.get(endpoint,headers=as_('ana'),params={'view':'flow','direction':'upstream','depth':16}).json()
    assert not any(n['kind']=='api' for n in hidden['nodes'])


def test_file_landing_flow_keeps_end_to_end_path_when_batches_exceed_graph_limit():
    from types import SimpleNamespace
    from app.models import LineageEdge
    namespace = catalog.asset_id('landing-schema')
    nodes = {namespace:SimpleNamespace(id=namespace,resource_id='landing-schema',kind='schema')}
    for name,kind in [('api','api'),('extract','script'),('load','script'),('app','app')]:
        nodes[name] = SimpleNamespace(id=name,resource_id=name,kind=kind)
    edges = []

    def link(a,b,relation):
        edges.append(LineageEdge(id=str(len(edges)),source_id=a,target_id=b,relation=relation,
            origin='program_runtime',revision='test',observed_at=utcnow()))

    link('api','extract','reads')
    for i in range(300):
        name = f'file-{i}'
        nodes[name] = SimpleNamespace(id=name,resource_id='landing-schema',kind='file')
        link('extract',name,'writes'); link(name,'load','reads')
    link('load','app','writes')
    flow = catalog.data_flow_edges(edges,nodes,'app')
    assert len(flow) == 4
    assert {(e['source'],e['target']) for e in flow} == {
        ('api','extract'),('extract',namespace),(namespace,'load'),('load','app')}
    assert len(next(e for e in flow if e['target']==namespace)['evidence']) == 300
    focused = catalog.data_flow_edges(edges,nodes,'file-0')
    assert any(e['source']=='file-0' for e in focused)
    assert any(e['target']=='file-0' for e in focused)
    # An unrelated producer/consumer pair in the same namespace must not
    # invent a dependency between those pipelines by collapsing their files.
    for name in ('other-writer','other-reader'):
        nodes[name] = SimpleNamespace(id=name,resource_id=name,kind='script')
    nodes['unrelated-file'] = SimpleNamespace(id='unrelated-file',resource_id='landing-schema',kind='file')
    link('other-writer','unrelated-file','writes'); link('unrelated-file','other-reader','reads')
    separate = catalog.data_flow_edges(edges,nodes,'app')
    assert not any(e['source']==namespace or e['target']==namespace for e in separate)
    assert len(separate) == 604
    edges = edges[:-2]
    del nodes[namespace]
    assert len(catalog.data_flow_edges(edges,nodes,'app')) == 602


def test_failed_or_ambiguous_api_audit_events_do_not_invent_lineage(client, repo):
    from app.models import AuditEvent
    _,rid=register(client,repo,kind='script')
    api=client.post('/v1/apis',headers=as_('admin'),json={'name':'API','slug':'external','direction':'external','outbound':{'base_url':'https://example.com'}}).json()
    with SessionLocal() as db:
        p=db.scalar(select(Workload).where(Workload.resource_id==rid))
        db.add(AuditEvent(actor='service:'+p.service_principal_id,action='api.egress',target=api['id'],detail={'status':503}))
        db.add(AuditEvent(actor='ana',action='api.egress',target=api['id'],detail={'status':200}))
        catalog.sync_registry(db);catalog.harvest_api_calls(db)
        assert not list(db.scalars(select(LineageEdge).where(LineageEdge.origin=='api_runtime')))


from .test_apis import backend  # noqa: E402,F401


def test_managed_api_calls_record_live_lineage_without_exposing_request_parameters(client,repo,backend):
    pid,rid=register(client,repo,kind='script',slug='api-ingestion')
    external=client.post('/v1/apis',headers=as_('admin'),json={'name':'Upstream API','slug':'upstream','direction':'external','outbound':{'base_url':backend}}).json()
    client.post(f"/v1/apis/{external['id']}/operations",headers=as_('admin'),json={'name':'cards','method':'GET','path':'/cards/{id}'})
    with SessionLocal() as db:
        pub=db.get(Workload,pid)
        principal_id=pub.service_principal_id
        token=mint_token('execution',pub.identity,60,pub=pub.id,pid=principal_id,sha=pub.deployed_sha)
    assert client.put(f"/v1/rbac/resources/{external['resource_id']}/assignments",headers=as_('admin'),json={'principal_id':principal_id,'role':'Consumer'}).status_code==200
    for _ in range(2):
        response=client.get('/v1/egress/upstream/cards/7?private=do-not-index',headers={'Authorization':'Bearer '+token})
        assert response.status_code==200,response.text
    with SessionLocal() as db:
        links=list(db.scalars(select(LineageEdge).where(LineageEdge.origin=='api_runtime')))
        assert len(links)==1
        assert links[0].source_id==catalog.asset_id(external['resource_id'])
        assert links[0].target_id==catalog.asset_id(rid)
        assert links[0].revision=='a'*40
        assert all('do-not-index' not in str(a.attributes) for a in db.scalars(select(CatalogAsset)))


def test_both_directions_do_not_turn_at_shared_dependencies(client, repo):
    _, rid = register(client, repo, kind='script', slug='focus')
    asset(client, rid)
    with SessionLocal() as db:
        resource = db.get(Resource, rid)
        nodes = {name: catalog.upsert(db, resource, key=name, name=name, kind='script')
                 for name in ('input', 'focus', 'sibling', 'output', 'other-input')}
        for source, target in [('input', 'focus'), ('input', 'sibling'), ('focus', 'output'), ('other-input', 'output')]:
            catalog.edge(db, resource, nodes[source], nodes[target], 'reads', 'program_declared', 'sha')
        focus = nodes['focus'].id
        db.commit()
    for view in ('flow', 'full'):
        result = client.get(f'/v1/catalog/assets/{focus}/lineage', headers=as_('ana'),
                            params={'view': view, 'direction': 'both', 'depth': 16}).json()
        assert {n['name'] for n in result['nodes']} == {'input', 'focus', 'output'}


def test_orchestration_is_an_authorized_overlay_on_selected_data_work(client, repo):
    _, model_rid = register(client, repo)
    _, app_rid = register(client, repo, kind='app', slug='dashboard')
    _, scheduler_rid = register(client, repo, kind='airflow', slug='scheduler')
    # Keep the data path in the selected group when its broad scheduler-folder grant is revoked.
    for resource_id in (model_rid,app_rid):
        grant_group(client,resource_id,'Finance data','Owner',users=['ana'])
    assert client.post(f'/v1/catalog/resources/{model_rid}/import/dbt', headers=as_('ana'), json={'manifest': manifest()}).status_code == 200
    with SessionLocal() as db:
        catalog.sync_registry(db)
        model = db.scalar(select(CatalogAsset).where(CatalogAsset.kind == 'dbt_model'))
        table = db.scalar(select(CatalogAsset).where(CatalogAsset.kind == 'table', CatalogAsset.name == 'revenue'))
        resource = db.get(Resource, scheduler_rid)
        project = db.get(CatalogAsset, catalog.asset_id(model_rid))
        app = db.get(CatalogAsset, catalog.asset_id(app_rid))
        catalog.edge(db, db.get(Resource, app_rid), table, app, 'reads', 'program_declared', 'sha')
        dag = catalog.upsert(db, resource, key='dag', name='daily', kind='airflow_dag')
        tasks = {name: catalog.upsert(db, resource, key=name, name=name, kind='airflow_task') for name in ('models', 'verify', 'unrelated')}
        other = catalog.upsert(db, resource, key='other', name='other-program', kind='program')
        for task in tasks.values():
            catalog.edge(db, resource, dag, task, 'contains', 'airflow_dag', 'sha')
        for name in ('models', 'verify'):
            catalog.edge(db, resource, tasks[name], project, 'orchestrates', 'airflow_dag', 'sha')
        catalog.edge(db, resource, tasks['models'], tasks['verify'], 'depends_on', 'airflow_dag', 'sha')
        catalog.edge(db, resource, tasks['unrelated'], other, 'orchestrates', 'airflow_dag', 'sha')
        ids = {'dag': dag.id, 'model': model.id, 'project': project.id, **{k: v.id for k, v in tasks.items()}}
        db.commit()
    endpoint = f'/v1/catalog/assets/{catalog.asset_id(app_rid)}/lineage'
    params = {'view': 'flow', 'direction': 'both', 'depth': 16}
    plain = client.get(endpoint, headers=as_('ana'), params=params).json()
    overlay = client.get(endpoint, headers=as_('ana'), params={**params, 'orchestration': True}).json()
    assert {n['id'] for n in overlay['nodes']} - {n['id'] for n in plain['nodes']} == {ids[k] for k in ('dag', 'project', 'models', 'verify')}
    assert [e for e in overlay['edges'] if not e.get('control')] == plain['edges']
    assert sum(e['relation'] == 'triggers' and e['control'] for e in overlay['edges']) == 2
    assert next(n for n in overlay['nodes'] if n['id'] == ids['model'])['lineage_parent_id'] == ids['project']
    assert next(n for n in overlay['nodes'] if n['id'] == ids['models'])['lineage_parent_id'] == ids['dag']
    filtered = client.get(endpoint, headers=as_('ana'), params={**params, 'orchestration': True, 'origin': 'dbt_manifest'}).json()
    assert not any(e.get('control') for e in filtered['edges'])
    # Losing access to the scheduler removes its nodes and edges, including wrapper projects.
    with SessionLocal() as db:
        from app.models import RoleAssignment, ServiceRoleAssignment
        for assignment in db.scalars(select(RoleAssignment).where(RoleAssignment.resource_id.in_([scheduler_rid, 'workloads']))):
            db.delete(assignment)
        for assignment in db.scalars(select(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id == scheduler_rid)):
            db.delete(assignment)
        db.commit()
    hidden = client.get(endpoint, headers=as_('ana'), params={**params, 'orchestration': True}).json()
    assert hidden['nodes'] == plain['nodes'] and hidden['edges'] == plain['edges']
