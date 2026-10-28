from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app import catalog as c, measurements as m, rbac, sla_monitor, sql_access
from app.auth import User
from app.config import get_settings
from app.db import SessionLocal
from app.models import CatalogAsset, DatasetObservation, MetricDefinition, MetricObservation, Workload, Resource, SlaEvaluation, SlaIncident, SqlQuery, utcnow
from .conftest import as_
from .test_catalog import register, manifest, repo


@pytest.fixture
def product(client, repo):
    pid, rid = register(client, repo)
    response = client.post('/v1/catalog/resources/'+rid+'/import/dbt', headers=as_('ana'), json={'manifest':manifest()})
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        asset = db.scalar(select(CatalogAsset).where(CatalogAsset.kind == 'table', CatalogAsset.name == 'revenue'))
        return client, asset.id, pid, rid


def create_metric(client, aid, **changes):
    body = {'key':'coverage', 'name':'Card coverage', 'kind':'quality', 'unit':'%', 'minimum':90, 'warn_minimum':98, **changes}
    response = client.post(f'/v1/catalog/assets/{aid}/metrics', headers=as_('admin'), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_metric_definitions_observations_history_and_iam(product):
    client, aid, _, _ = product
    metric = create_metric(client, aid)
    path=f'/v1/catalog/assets/{aid}/metrics/{metric["id"]}'
    assert client.post(f'/v1/catalog/assets/{aid}/metrics',headers=as_('ana'),json={'key':'denied','name':'Denied'}).status_code==403
    for value, state in [(99,'healthy'),(95,'at_risk'),(80,'breached')]:
        response=client.post(path+'/observations',headers=as_('admin'),json={'value':value,'idempotency_key':str(value),'numerator':value,'denominator':100})
        assert response.status_code==201,response.text
        assert response.json()['status']==state
    client.post(path+'/observations',headers=as_('admin'),json={'value':80,'idempotency_key':'80'})
    history=client.get(path+'/history',headers=as_('ana'))
    assert history.status_code==200 and len(history.json())==3
    assert history.json()[0]['detail']['thresholds']['minimum']==90
    assert client.get(path+'/history',headers=as_('bob')).status_code==404
    assert client.post(path+'/observations',headers=as_('ana'),json={'value':100}).status_code==403
    changed=create_metric(client,aid,minimum=95,warn_minimum=99)
    assert changed['version']==2 and changed['latest']['definition_version']==1
    with SessionLocal() as db:
        asset=db.get(CatalogAsset,aid)
        assert asset.last_success_at is None  # quality assertions cannot make data fresh
        metric_asset=db.get(CatalogAsset,c.asset_id(asset.resource_id,'metric:'+metric['id']))
        assert metric_asset.kind=='quality_metric'
    assert client.post(path+'/observations',headers=as_('admin'),json={'value':1,'denominator':0}).status_code==422


def test_sql_measurements_use_trino_and_only_declared_reader_scopes(product, monkeypatch):
    client, aid, _, _ = product
    monkeypatch.setattr(get_settings(),'query_executor','trino')
    monkeypatch.setattr(get_settings(),'trino_signing_key','test-trino-signing-key-32-characters')
    monkeypatch.setattr(sql_access,'check_native_relations',lambda q:None)
    with SessionLocal() as db:
        resource=db.get(Resource,db.get(CatalogAsset,aid).resource_id)
        resource.config={**resource.config,'state':'ready'}
        db.commit()
    sent=[]
    async def engine(method,path,**kwargs):
        sent.append(kwargs)
        return httpx.Response(200,json={'id':'metric_query','columns':[{'name':'value'}],'data':[[98.7]],'stats':{'state':'FINISHED'}})
    monkeypatch.setattr(sql_access,'engine_request',engine)
    metric=create_metric(client,aid,source='finance-dw',sql='SELECT count(*) FROM finance_dw.public.revenue')
    response=client.post(f'/v1/catalog/assets/{aid}/metrics/{metric["id"]}/measure',headers=as_('admin'))
    assert response.status_code==200,response.text
    assert response.json()['value']==98.7 and response.json()['status']=='healthy'
    with SessionLocal() as db:
        query=db.scalar(select(SqlQuery).where(SqlQuery.engine_query_id=='metric_query'))
        assert set(query.scope)=={'finance_dw'}
        assert query.scope['finance_dw']['schemas']=={'public':'Reader'}
        assert query.scope['finance_dw']['create_schemas']==[]
    assert len(sent)==1
    for sql in ['DELETE FROM finance.public.revenue','SELECT 1; DELETE FROM finance.public.revenue',"EXECUTE IMMEDIATE 'DELETE FROM finance.public.revenue'"]:
        assert client.post(f'/v1/catalog/assets/{aid}/metrics',headers=as_('admin'),json={'key':'bad','name':'Bad','source':'finance-dw','sql':sql}).status_code==422


def test_scheduled_deadlines_are_evaluated_without_runs_and_alerts_resolve(product):
    client, aid, _, _=product
    now=datetime(2026,1,6,6,16,tzinfo=timezone.utc)  # 07:16 Europe/Amsterdam
    with SessionLocal() as db:
        asset=db.get(CatalogAsset,aid);resource=db.get(Resource,asset.resource_id)
        asset.sla={'deadline_time':'07:00','timezone':'Europe/Amsterdam','weekdays':[0,1,2,3,4],
            'grace_minutes':15,'warning_minutes':30,'starts_at':'2026-01-05T08:00:00+00:00','window_hours':168}
        first=sla_monitor.record_evaluation(db,asset,resource,now)
        assert first['status']=='breached'
        assert first['checks'][0]['samples']==1 and first['checks'][0]['compliance_percent']==0
        sla_monitor.record_evaluation(db,asset,resource,now+timedelta(minutes=1))
        db.commit()
        assert db.scalar(select(func.count()).select_from(SlaEvaluation).where(SlaEvaluation.asset_id==aid))==1
    alerts=client.get('/v1/catalog/alerts?asset_id='+aid,headers=as_('admin')).json()
    assert len(alerts)==1
    assert client.get('/v1/catalog/alerts',headers=as_('bob')).json()==[]
    assert client.post('/v1/catalog/alerts/'+alerts[0]['id']+'/acknowledge',headers=as_('ana')).status_code==403
    assert client.post('/v1/catalog/alerts/'+alerts[0]['id']+'/acknowledge',headers=as_('admin')).status_code==200
    with SessionLocal() as db:
        asset=db.get(CatalogAsset,aid)
        at=datetime(2026,1,6,5,55,tzinfo=timezone.utc)
        m.dataset_observation(db,asset,at,'test','batch-1')
        result=sla_monitor.record_evaluation(db,asset,db.get(Resource,asset.resource_id),now)
        assert result['status']=='healthy' and result['checks'][0]['compliance_percent']==100
        db.commit()
    assert client.get('/v1/catalog/alerts?asset_id='+aid,headers=as_('admin')).json()==[]
    assert len(client.get(f'/v1/catalog/assets/{aid}/sla-history',headers=as_('ana')).json())==2


def test_warning_unknown_quality_and_source_checks_are_distinct(product):
    client,aid,_,_=product
    metric=create_metric(client,aid)
    with SessionLocal() as db:
        asset=db.get(CatalogAsset,aid);resource=db.get(Resource,asset.resource_id)
        now=utcnow();asset.last_success_at=now-timedelta(minutes=50)
        asset.sla={'freshness_minutes':60,'quality_required':True,'quality_max_age_minutes':60}
        assert sla_monitor.evaluate(db,asset,resource,now)['status']=='unknown'
        m.observe(db,db.get(MetricDefinition,metric['id']),100,at=now-timedelta(minutes=1))
        assert sla_monitor.evaluate(db,asset,resource,now)['status']=='at_risk'
        assert sla_monitor.evaluate(db,asset,resource,now+timedelta(minutes=20))['status']=='breached'
        m.dataset_observation(db,asset,now,'source','check-1',kind='checked')
        asset.sla={**asset.sla,'freshness_basis':'checked'}
        assert sla_monitor.evaluate(db,asset,resource,now)['status']=='healthy'
        assert asset.last_success_at==now-timedelta(minutes=50)
        assert sla_monitor.evaluate(db,asset,resource,now+timedelta(minutes=61))['status']=='breached'


def test_sla_input_gates_are_explicit_and_can_be_repaired(product):
    client,aid,pid,rid=product
    with SessionLocal() as db:
        asset=db.get(CatalogAsset,aid);asset.sla={'freshness_minutes':60}
        asset.last_success_at=utcnow()-timedelta(hours=2)
        root=c.upsert(db,db.get(Resource,rid));root.sla={'enforcement':'block_new_runs','required_assets':[aid],'block_on_unknown':True}
        db.flush();pub=db.get(Workload,pid);admin=rbac.resolve_user(db,User('admin'))
        with pytest.raises(HTTPException) as error:
            sla_monitor.require_inputs(db,pub,admin)
        assert error.value.status_code==409 and error.value.detail['code']=='SLA_INPUT_GATE'
        asset.last_success_at=utcnow()
        sla_monitor.require_inputs(db,pub,admin)
        root.sla={'enforcement':'monitor','required_assets':[aid]}
        asset.last_success_at=None
        sla_monitor.require_inputs(db,pub,admin)


def test_dbt_quality_results_are_automatic_idempotent_and_preserve_history(product):
    client,aid,_,rid=product
    artifact=manifest()
    uid='test.finance.card_ids_unique'
    artifact['nodes'][uid]={'name':'card_ids_unique','resource_type':'test','depends_on':{'nodes':['model.finance.revenue']},'config':{'severity':'error'}}
    path='/v1/catalog/resources/'+rid+'/import/dbt'
    def ingest(status,failures,invocation):
        return client.post(path,headers=as_('admin'),json={'manifest':artifact,'run_results':{'metadata':{'invocation_id':invocation,'generated_at':(utcnow()-timedelta(seconds=1)).isoformat()},'results':[{'unique_id':uid,'status':status,'failures':failures}]}})
    payload={'manifest':artifact,'run_results':{'metadata':{'invocation_id':'first','generated_at':'2026-01-01T00:00:00Z'},'results':[{'unique_id':uid,'status':'fail','failures':3}]}}
    for _ in range(2):
        response=client.post(path,headers=as_('admin'),json=payload)
        assert response.status_code==200,response.text
    metrics=client.get(f'/v1/catalog/assets/{aid}/metrics',headers=as_('ana')).json()
    assert len(metrics)==1 and metrics[0]['origin']=='dbt' and metrics[0]['latest']['value']==3
    metric=metrics[0]
    assert len(client.get(f'/v1/catalog/assets/{aid}/metrics/{metric["id"]}/history',headers=as_('ana')).json())==1
    assert ingest('pass',0,'second').status_code==200
    rows=client.get(f'/v1/catalog/assets/{aid}/metrics/{metric["id"]}/history',headers=as_('ana')).json()
    assert [r['status'] for r in rows]==['healthy','breached']
    with SessionLocal() as db:
        assert db.get(CatalogAsset,aid).last_success_at is None


def test_sla_policy_validates_timezone_schedule_and_missing_gates(product):
    client,aid,_,_=product
    path=f'/v1/catalog/assets/{aid}/sla'
    for body in [{'timezone':'No/SuchZone'}, {'deadline_time':'25:00'}, {'weekdays':[7]}, {'weekdays':[]}, {'enforcement':'block_new_runs'}]:
        assert client.put(path,headers=as_('admin'),json=body).status_code==422
    response=client.put(path,headers=as_('admin'),json={'deadline_time':'07:00','timezone':'Europe/Amsterdam','weekdays':[0,1,2,3,4],'grace_minutes':15})
    assert response.status_code==200,response.text
    assert response.json()['sla']['starts_at']


def test_metric_and_input_gate_dependencies_cannot_cross_prod_boundary(product):
    from app.layers import PROD
    client,aid,pid,rid=product
    with SessionLocal() as db:
        resource=Resource(kind='schema',name='prod_analytics',object_id='prod-source',config={'layer_id':PROD})
        db.add(resource);db.flush()
        prod=c.upsert(db,resource,'object:cards',kind='table',name='prod_cards')
        prod_id=prod.id
        db.commit()
    response=client.post(f'/v1/catalog/assets/{aid}/metrics',headers=as_('admin'),json={'key':'cross','name':'Cross','dependencies':[prod_id]})
    assert response.status_code==422 and 'same layer' in response.text
    response=client.put(f'/v1/catalog/assets/{c.asset_id(rid)}/sla',headers=as_('admin'),json={'required_assets':[prod_id],'enforcement':'block_new_runs'})
    assert response.status_code==422 and 'same layer' in response.text


def test_deadline_uses_daylight_saving_timezone_and_not_server_clock(product):
    _,aid,_,_=product
    with SessionLocal() as db:
        asset=db.get(CatalogAsset,aid)
        asset.sla={'deadline_time':'07:00','timezone':'Europe/Amsterdam','starts_at':'2026-03-28T10:00:00Z','grace_minutes':15}
        result=sla_monitor.evaluate(db,asset,db.get(Resource,asset.resource_id),datetime(2026,3,29,5,16,tzinfo=timezone.utc))
        check=result['checks'][0]
        assert check['status']=='breached' and check['deadline_at']=='2026-03-29T05:00:00+00:00'


def test_removed_dbt_tests_retire_definitions_but_keep_results(product):
    client,aid,_,rid=product
    artifact=manifest()
    artifact['nodes']['test.finance.unique']={'name':'unique','resource_type':'test','depends_on':{'nodes':['model.finance.revenue']},'config':{}}
    path='/v1/catalog/resources/'+rid+'/import/dbt'
    response=client.post(path,headers=as_('admin'),json={'manifest':artifact,'run_results':{'metadata':{'generated_at':'2026-01-01T00:00:00Z'},'results':[{'unique_id':'test.finance.unique','status':'pass','failures':0}]}})
    assert response.status_code==200,response.text
    metric=client.get(f'/v1/catalog/assets/{aid}/metrics',headers=as_('ana')).json()[0]
    client.post(path,headers=as_('admin'),json={'manifest':manifest()}).raise_for_status()
    retired=client.get(f'/v1/catalog/assets/{aid}/metrics',headers=as_('ana')).json()[0]
    assert not retired['enabled']
    assert len(client.get(f'/v1/catalog/assets/{aid}/metrics/{metric["id"]}/history',headers=as_('ana')).json())==1


def test_descriptive_import_permission_cannot_forge_dataset_health(product):
    client,aid,_,rid=product
    now=(utcnow()-timedelta(seconds=1)).isoformat()
    body={'manifest':manifest(),'run_results':{'metadata':{'invocation_id':'untrusted'},'results':[{'unique_id':'model.finance.revenue','status':'success','timing':[{'name':'execute','completed_at':now}]}]}}
    response=client.post('/v1/catalog/resources/'+rid+'/import/dbt',headers=as_('ana'),json=body)
    assert response.status_code==200,response.text
    with SessionLocal() as db:
        assert db.get(CatalogAsset,aid).last_success_at is None
        assert db.scalar(select(func.count()).select_from(DatasetObservation).where(DatasetObservation.asset_id==aid))==0


def test_input_gate_blocks_new_run_admission_before_creating_a_job(product, repo):
    from .test_deploying import deploy
    client,aid,_,_=product
    created=deploy(client,repo,user='ana',program='sales-report',slug='sla-consumer')
    assert created.status_code==201,created.text
    pub=created.json()
    client.get('/v1/catalog/search',headers=as_('admin')).raise_for_status()
    client.put(f'/v1/catalog/assets/{aid}/sla',headers=as_('admin'),json={'freshness_minutes':60}).raise_for_status()
    path=f'/v1/catalog/assets/{c.asset_id(pub["resource_id"])}/sla'
    policy=client.put(path,headers=as_('admin'),json={'required_assets':[aid],'enforcement':'block_new_runs'})
    assert policy.status_code==200,policy.text
    with SessionLocal() as db:
        from app.models import Run
        before=db.scalar(select(func.count()).select_from(Run))
    response=client.post('/v1/workloads/'+pub['id']+'/runs',headers=as_('ana'),json={'args':{}})
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']=='SLA_INPUT_GATE'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Run))==before


def test_catalog_metric_discovery_uses_bounded_database_queries(product):
    """A large dbt test suite must not trigger permission queries per metric."""
    from sqlalchemy import event
    from app.db import engine
    _, aid, _, _ = product
    with SessionLocal() as db:
        parent = db.get(CatalogAsset, aid)
        resource = db.get(Resource, parent.resource_id)
        for index in range(100):
            c.upsert(db, resource, 'bulk-metric:'+str(index), kind='kpi', name='Bulk metric '+str(index),
                     attributes={'dependency_asset_ids':[aid]})
        missing = c.upsert(db, resource, 'missing-dependency', kind='kpi', name='Missing dependency',
                           attributes={'dependency_asset_ids':['missing']}).id
        db.commit()

    def discover(condition):
        queries = []
        def count(connection, cursor, statement, parameters, context, executemany):
            queries.append(statement)
        with SessionLocal() as db:
            event.listen(engine, 'before_cursor_execute', count)
            try:
                assets, _ = c.visible_assets(db, User('admin', roles=['fdp-admin']), condition=condition)
                return {a.id for a in assets}, len(queries)
            finally:
                event.remove(engine, 'before_cursor_execute', count)

    single, small_queries = discover(CatalogAsset.name == 'Bulk metric 0')
    many, large_queries = discover(CatalogAsset.kind == 'kpi')
    assert len(single) == 1 and len(many) == 100 and missing not in many
    assert large_queries <= small_queries + 3, (small_queries, large_queries)


def test_background_sla_tick_skips_unconfigured_assets_and_resolves_removed_policy(product):
    _, aid, _, _ = product
    with SessionLocal() as db:
        asset = db.get(CatalogAsset, aid)
        asset.sla = {'freshness_minutes':60}
        db.commit()
    sla_monitor.tick(SessionLocal)
    with SessionLocal() as db:
        incident = db.scalar(select(SlaIncident).where(SlaIncident.asset_id == aid))
        assert incident and incident.status == 'unknown' and incident.resolved_at is None
        assert db.scalar(select(func.count()).select_from(SlaEvaluation)) == 1
        db.get(CatalogAsset, aid).sla = {}
        db.commit()
    sla_monitor.tick(SessionLocal)
    with SessionLocal() as db:
        incident = db.scalar(select(SlaIncident).where(SlaIncident.asset_id == aid))
        assert incident.status == 'resolved' and incident.resolved_at
        assert db.scalar(select(func.count()).select_from(SlaEvaluation)) == 2
