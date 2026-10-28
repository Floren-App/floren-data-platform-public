"""Environment isolation is independent of a human's union of identity memberships."""
from sqlalchemy import select
from app import layers, rbac, deps, sql_access
from app.auth import User, mint_token
from app.db import SessionLocal
from app.models import DataSource, Resource
from .conftest import as_
from .rbac_helpers import grant_group


def environments(client):
    result=[]
    for name,branch,workspaces in [('Test','test',True),('Prod','main',False)]:
        response=client.post('/v1/rbac/folders',headers=as_('admin'),json={
            'name':name,'environment':True,'branch':branch,'workspaces':workspaces})
        assert response.status_code==201,response.text
        result.append(response.json())
    return result


def test_workspace_legacy_environment_selection_uses_global_layer_policy_and_private_volumes(client):
    from .test_api import onboard_workspace_group
    onboard_workspace_group(client,'ana')
    test,prod=environments(client)
    base={'name':'Workspace','editor':'vscode'}
    assert client.post('/v1/workspaces',headers=as_('ana'),json=base).status_code==201
    assert client.post('/v1/workspaces',headers=as_('ana'),json={**base,'environment_id':test['id']}).status_code==201
    grant_group(client,test['id'],'Developers','Contributor',users=['ana'])
    grant_group(client,prod['id'],'Viewers','Executor',users=['ana'])
    created=client.post('/v1/workspaces',headers=as_('admin'),json={**base,'environment_id':prod['id']})
    assert created.status_code==201 and created.json()['layer_id']==layers.PROD
    volumes=[]
    for name in ('One','Two'):
        response=client.post('/v1/workspaces',headers=as_('ana'),json={**base,'name':name,'environment_id':test['id']})
        assert response.status_code==201,response.text
        ws=response.json()
        assert client.post(f"/v1/workspaces/{ws['id']}/start",headers=as_('ana')).status_code==200
        volumes.append(deps.get_orchestrator().executions[f"ws-{ws['id']}"].home_volume)
    assert len(set(volumes))==2


def test_admin_workload_cannot_escape_environment_through_sql_iam_identity_or_folder_move(client):
    test,prod=environments(client)
    for env in (test,prod):
        grant_group(client,env['id'],env['name']+' owners','Owner',users=['admin'])
    ws=client.post('/v1/workspaces',headers=as_('admin'),json={'name':'Test','editor':'vscode','environment_id':test['id']}).json()
    client.post(f"/v1/workspaces/{ws['id']}/start",headers=as_('admin'))
    token=mint_token('execution','admin',3600,ws=ws['id'])
    bearer={'Authorization':'Bearer '+token}
    assert client.post('/v1/rbac/folders',headers=bearer,json={'name':'Escape','parent_id':prod['id']}).status_code==403
    assert client.post('/v1/rbac/groups',headers=bearer,json={'name':'Escape','owners':['fake']}).status_code==403
    with SessionLocal() as db:
        from app.environments import execution_user
        admin=rbac.resolve_user(db,User('admin'))
        scoped=rbac.resolve_user(db,execution_user(db,{'sub':'admin','ws':ws['id']}))
        assert scoped.environment_id==layers.NON_PROD and not scoped.is_admin
        for env in (test,prod):
            ds=DataSource(name=env['name'].lower(),kind='postgres',config={'catalog':env['name'].lower(),'environment_id':env['id']})
            db.add(ds);db.flush()
            root=rbac.source_resource(db,ds)
            db.add(Resource(kind='schema',name='analytics',parent_id=root.id,object_id=ds.id,config={'state':'ready'}))
            db.flush();rbac.own(db,admin,root)
        db.commit()
        assert set(sql_access.scopes(db,sql_access.actor(db,'Bearer '+token)))=={'test'}
        source=db.scalar(select(DataSource).where(DataSource.name=='prod'))
        assert rbac.data_scope(db,scoped,source)=={}
        assert not rbac.allowed(db,scoped,'assign',rbac.source_resource(db,source))
    response=client.put('/v1/admin/service-accounts/prod-account',headers=as_('admin'),json={'environment_id':prod['id']})
    assert response.status_code==200,response.text
    assert client.post('/v1/service-accounts/prod-account/tokens',headers=bearer,json={'name':'escape'}).status_code==403
    assert client.patch(f"/v1/rbac/resources/{test['id']}",headers=as_('admin'),json={'name':'Moved','parent_id':'workloads'}).status_code==409


def test_service_cross_environment_grants_are_rejected(client):
    test,prod=environments(client)
    with SessionLocal() as db:
        p=rbac.principal(db,'service','test-service','Test service','local')
        db.add(Resource(kind='service',name='Test service',object_id=p.external_id,parent_id=test['id']))
        db.commit();pid=p.id
    for env,status in [(prod,422),(test,200)]:
        response=client.put(f"/v1/rbac/resources/{env['id']}/assignments",headers=as_('admin'),json={'principal_id':pid,'role':'Executor'})
        assert response.status_code==status,response.text


def test_directory_reads_do_not_take_exclusive_locks(client):
    from sqlalchemy import text
    from app.db import engine
    if engine.dialect.name != 'postgresql':
        import pytest
        pytest.skip('requires independent PostgreSQL transactions')
    client.get('/v1/me',headers=as_('admin'))
    with SessionLocal() as first, SessionLocal() as second:
        user=rbac.resolve_user(first,User('admin'))
        second.execute(text("SET LOCAL lock_timeout='500ms'"))
        # The first read transaction remains open; a second read must still finish.
        assert rbac.resolve_user(second,User('admin')).principal_id==user.principal_id


def test_catalog_scope_cache_and_parent_grants_respect_execution_environment(client):
    from dataclasses import replace
    from app import catalog
    test,prod=environments(client)
    grant_group(client,'workloads','Project owners','Owner',users=['admin'])
    with SessionLocal() as db:
        admin=rbac.resolve_user(db,User('admin'))
        test_actor=replace(admin,environment_id=test['id'],execution_context='workspace:test')
        prod_actor=replace(admin,environment_id=prod['id'],execution_context='workload:prod')
        _,actions=catalog.permissions(db,test_actor)
        assert 'assign' in actions[test['id']]
        assert not actions[prod['id']]
        _,actions=catalog.permissions(db,prod_actor)
        assert 'assign' in actions[prod['id']]
        assert not actions[test['id']]


def test_lineage_producers_cannot_update_other_environment_datasets(client):
    from app.catalog_ingest import dataset
    test,prod=environments(client)
    with SessionLocal() as db:
        producer=Resource(kind='service',name='Test ingest',parent_id=test['id'])
        db.add(producer)
        for env in (test,prod):
            source=DataSource(name=env['name'].lower(),kind='postgres',config={'environment_id':env['id']})
            db.add(source);db.flush();root=rbac.source_resource(db,source)
            db.add(Resource(kind='schema',name='raw',parent_id=root.id,object_id=source.id));db.flush()
        assert dataset(db,producer,{'source':'prod','schema':'raw','name':'cards'}) is None
        assert dataset(db,producer,{'source':'test','schema':'raw','name':'cards'}) is not None
        assert dataset(db,producer,{'source':'test','schema':'raw','name':'cards__dbt_tmp'}) is None


def test_workloads_cannot_use_another_environment_branch(client):
    test,prod=environments(client)
    for environment,wrong_branch in [(test,'main'),(prod,'test')]:
        response=client.post('/v1/workloads',headers=as_('admin'),json={
            'repo_id':'unused','folder_id':environment['id'],'branch':wrong_branch,
            'program':'ingest','slug':'wrong-branch'})
        assert response.status_code==422,response.text
        assert 'requires branch' in response.text
