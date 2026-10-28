"""Container graphs preserve native model structure and canonical object identity."""
from datetime import timedelta

from sqlalchemy import select

from app import catalog, rbac
from app.auth import User
from app.db import SessionLocal
from app.models import CatalogAsset, SqlQuery, utcnow
from .conftest import as_
from .test_catalog import asset, manifest, register
from .test_deploying import repo  # noqa: F401


def project(client, repo):
    pid, rid = register(client, repo)
    base = asset(client, rid)
    doc = manifest()
    doc['nodes']['model.finance.current'] = {
        **doc['nodes']['model.finance.revenue'], 'name':'current', 'alias':'current',
        'depends_on':{'nodes':['model.finance.revenue']}}
    response = client.post(f'/v1/catalog/resources/{rid}/import/dbt', headers=as_('ana'), json={'manifest':doc})
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
        for table in ('sales','revenue','current'):
            query = SqlQuery(principal_id=ana.principal_id, actor='ana', binding='test', workload_id=pid,
                state='FINISHED', expires_at=utcnow()+timedelta(minutes=10), lineage={table:{
                    'catalog':'finance_dw','schema':'public','table':table,
                    'relations':['reads'] if table=='sales' else ['reads','writes']}})
            db.add(query); db.flush()
            catalog.successful_query(db, query)
        db.commit()
    return rid, base


def test_project_focus_groups_models_without_runtime_cycles_or_intermediate_table_duplicates(client, repo):
    rid, base = project(client, repo)
    result = client.get(f"/v1/catalog/assets/{base['id']}/lineage", headers=as_('ana'),
                        params={'view':'flow','direction':'upstream','depth':16}).json()
    nodes = {n['id']:n for n in result['nodes']}
    assert {(n['name'],n['kind']) for n in nodes.values()} == {
        ('models','dbt'),('sales','table'),('revenue','dbt_model'),('current','dbt_model'),('current','table')}
    models = [n for n in nodes.values() if n['kind']=='dbt_model']
    assert all(n['lineage_parent_id']==base['id'] for n in models)
    assert all(base['id'] not in (e['source'],e['target']) for e in result['edges'])
    assert any(e['relation']=='depends_on' for e in result['edges'])
    assert any(e['relation']=='writes' and nodes[e['target']]['kind']=='table' for e in result['edges'])
    full = client.get(f"/v1/catalog/assets/{base['id']}/lineage", headers=as_('ana'), params={'view':'full','depth':16}).json()
    assert any(e['origin']=='trino_runtime' and base['id'] in (e['source'],e['target']) for e in full['edges'])


def test_storage_and_catalog_resolve_same_asset_without_crawling_and_enforce_iam(client, repo, monkeypatch):
    import app.routes.data_browser as data_browser
    rid, base = project(client, repo)
    monkeypatch.setattr(data_browser, 'storage_children', lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected crawl')))
    with SessionLocal() as db:
        table = db.scalar(select(CatalogAsset).where(CatalogAsset.kind=='table',CatalogAsset.name=='sales'))
        schema_id, table_id = table.resource_id, table.id
        from app.models import Resource
        source_id = db.get(Resource,schema_id).parent_id
    resolved = client.get(f'/v1/catalog/resources/{schema_id}/asset', params={'object_path':'sales'}, headers=as_('ana'))
    assert resolved.status_code==200 and resolved.json()['id']==table_id
    assert client.get(f'/v1/catalog/resources/{rid}/asset',headers=as_('ana')).json()['id']==base['id']
    for resource_id in (schema_id,source_id):
        viewer = 'ana' if resource_id == schema_id else 'admin'
        response=client.get(f'/v1/catalog/resources/{resource_id}/asset',headers=as_(viewer))
        assert response.status_code==200
        focus=response.json()['id']
        graph=client.get(f'/v1/catalog/assets/{focus}/lineage',params={'view':'flow','direction':'upstream','depth':16},headers=as_(viewer)).json()
        assert any(n['id']==table_id and n.get('lineage_parent_id')==focus for n in graph['nodes'])
    assert client.get(f'/v1/catalog/resources/{source_id}/asset',headers=as_('ana')).status_code==404
    assert client.get(f'/v1/catalog/resources/{schema_id}/asset',params={'object_path':'sales'},headers=as_('bob')).status_code==404
    assert client.get(f'/v1/catalog/resources/{schema_id}/asset',params={'object_path':'missing'},headers=as_('ana')).status_code==404


def test_detailed_hierarchy_retains_materializations_and_only_authorized_ancestors(client, repo):
    rid, base = project(client, repo)
    endpoint=f"/v1/catalog/assets/{base['id']}/lineage"
    response=client.get(endpoint,headers=as_('ana'),params={'view':'flow','direction':'upstream','depth':16,'hierarchy':True})
    assert response.status_code==200, response.text
    result=response.json(); nodes={node['id']:node for node in result['nodes']}
    tables=[node for node in nodes.values() if node['kind']=='table']
    assert {node['name'] for node in tables}=={'sales','revenue','current'}
    for table in tables:
        assert nodes[table['lineage_parent_id']]['kind']=='schema'
    assert all(node['lineage_parent_id']==base['id'] for node in nodes.values() if node['kind']=='dbt_model')
    assert nodes[base['id']]['lineage_child_count']==2
    assert sum(edge['relation']=='writes' for edge in result['edges'])==2
    hidden=client.get(endpoint,headers=as_('bob'),params={'view':'flow','depth':16,'hierarchy':True})
    assert hidden.status_code==200
    assert all(node['kind'] not in {'table','schema'} for node in hidden.json()['nodes'])
    assert all('lineage_parent_id' not in node or node['lineage_parent_id'] in {a['id'] for a in hidden.json()['nodes']} for node in hidden.json()['nodes'])


def test_airflow_program_and_focused_task_have_intrinsic_nested_hierarchy(client, repo):
    _, rid=register(client,repo,kind='airflow',slug='pipelines')
    _, target=register(client,repo,kind='script',slug='load')
    base=asset(client,rid)
    body={'dag_id':'daily','tasks':[{'task_id':'extract'},{'task_id':'load','workload':'load','upstream_task_ids':['extract']}]}
    assert client.post(f'/v1/catalog/resources/{rid}/import/airflow',headers=as_('ana'),json=body).status_code==200
    params={'view':'flow','direction':'upstream','depth':16,'hierarchy':True}
    result=client.get(f"/v1/catalog/assets/{base['id']}/lineage",headers=as_('ana'),params=params).json()
    nodes={node['id']:node for node in result['nodes']}
    dag=next(node for node in nodes.values() if node['kind']=='airflow_dag')
    task=next(node for node in nodes.values() if node['kind']=='airflow_task' and node['name']=='load')
    assert dag['lineage_parent_id']==base['id']
    assert task['lineage_parent_id']==dag['id']
    assert any(edge['relation']=='triggers' and edge['target']==catalog.asset_id(target) for edge in result['edges'])
    focused=client.get(f"/v1/catalog/assets/{task['id']}/lineage",headers=as_('ana'),params=params).json()
    assert {node['name'] for node in focused['nodes'] if node['kind']=='airflow_task'}=={'extract','load'}
    assert any(node['id']==dag['id'] and node['lineage_parent_id']==base['id'] for node in focused['nodes'])

    # Evidence filtering changes connections, never an object's intrinsic ancestry.
    filtered=client.get(f"/v1/catalog/assets/{task['id']}/lineage",headers=as_('ana'),
        params={**params,'origin':'trino_runtime'}).json()
    ancestry={node['id']:node for node in filtered['nodes']}
    assert ancestry[task['id']]['lineage_parent_id']==dag['id']
    assert ancestry[dag['id']]['lineage_parent_id']==base['id']
    assert filtered['edges']==[]
