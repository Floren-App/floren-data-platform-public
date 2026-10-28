"""Desired-state concurrency, automation ownership, and fail-closed admission."""
import pytest
import json
from pathlib import Path
from pydantic import ValidationError
from sqlalchemy import select

from app import layer_infrastructure as infra, layers
from app.db import SessionLocal
from app.models import LayerOperation
from .conftest import as_
from .rbac_helpers import grant_group


def managed():
    return {'enabled':True, 'trino':{'workers':{'warehouse':'trino'}}}


def test_exported_infrastructure_schema_matches_public_contract(client):
    exported = Path(__file__).resolve().parents[3] / 'schemas/layer-infrastructure.schema.json'
    response = client.get('/v1/schema/layer-infrastructure.json')
    assert response.status_code == 200
    assert json.loads(exported.read_text()) == response.json()


def test_infrastructure_updates_are_versioned_idempotent_and_admin_only(client):
    path='/v1/admin/layers/'+layers.NON_PROD+'/infrastructure'
    assert client.get(path,headers=as_('ana')).status_code==403
    assert client.get(path,headers=as_('admin')).json()['generation']==0
    body={'expected_generation':0,'spec':managed()}
    response=client.put(path,headers=as_('admin'),json=body)
    assert response.status_code==202,response.text
    state=response.json()
    assert state['generation']==1 and state['operation']['phase']=='Pending'
    assert state['spec']['storage']['retention']=='Retain'
    assert client.put(path,headers=as_('admin'),json=body).status_code==409
    body['expected_generation']=1
    assert client.put(path,headers=as_('admin'),json=body).json()==state
    # An edit made through the old layer form must not discard infrastructure.
    assert client.put('/v1/admin/layers/'+layers.NON_PROD,headers=as_('admin'),json={'name':'Test','workspaces':True}).status_code==200
    assert client.get(path,headers=as_('admin')).json()['spec']==state['spec']
    assert len(client.get('/v1/admin/layers/'+layers.NON_PROD+'/operations',headers=as_('admin')).json())==1
    body['spec']['trino']['cpu']=2
    assert client.put(path,headers=as_('admin'),json=body).status_code==409
    with SessionLocal() as db:
        operation=db.scalar(select(LayerOperation).where(LayerOperation.layer_id==layers.NON_PROD))
        operation.phase='Failed'; db.commit()
    assert client.put(path,headers=as_('admin'),json=body).json()['generation']==2


@pytest.mark.parametrize('change',[
    {'trino':{'signing_key':{'name':'key','key':'value'}}},
    {'trino':{'internal_secret':{'name':'key','key':'value','namespace':'prod'}}},
    {'trino':{'image':'trinodb/trino:latest'}},
    {'trino':{'image':'trinodb/trino:484','cpu':0}},
    {'storage':{'retention':'Delete'}},
    {'postgres':{'enabled':False,'catalog':'warehouse'}},
    {'rustfs':{'enabled':True,'credentials_secret':'auth'}},
    {'trino':{'tls':{'mode':'existing'}}},
    {'trino':{'tls':{'mode':'cert_manager','secret_name':'tls','issuer':{'name':'issuer'},'duration_hours':24,'renew_before_hours':24}}},
    {'namespace':'production'},
    {'phase':'Ready'},
])
def test_infrastructure_rejects_unsafe_or_incomplete_contracts(change):
    with pytest.raises(ValidationError):
        infra.InfrastructureSpec.model_validate({**managed(),**change})


def test_managed_layer_cannot_start_workspaces_before_ready(client):
    path='/v1/admin/layers/'+layers.NON_PROD+'/infrastructure'
    assert client.put(path,headers=as_('admin'),json={'expected_generation':0,'spec':managed()}).status_code==202
    ws=client.post('/v1/workspaces',headers=as_('admin'),json={'name':'Waiting','editor':'vscode','layer_id':layers.NON_PROD}).json()
    result=client.post('/v1/workspaces/'+ws['id']+'/start',headers=as_('admin'))
    assert result.status_code==409 and result.json()['detail']['code']=='LayerNotReady'
    assert client.put(path,headers=as_('admin'),json={'expected_generation':1,'spec':{'enabled':False}}).status_code==409


def test_automation_primary_group_is_explicit_authorized_and_not_persisted(client):
    parent=client.post('/v1/rbac/folders',headers=as_('admin'),json={'name':'Automation','layer_id':layers.NON_PROD}).json()
    owners=grant_group(client,parent['id'],'Automation owners','Admin',users=['ana'])
    previous=client.get('/v1/me',headers=as_('ana')).json()['active_group_id']
    headers={**as_('ana'),'X-FDP-Active-Group':owners}
    result=client.post('/v1/rbac/folders',headers=headers,json={'name':'Created by automation','parent_id':parent['id']})
    assert result.status_code==201,result.text
    assert result.json()['owner_group_id']==owners
    assert client.get('/v1/me',headers=as_('ana')).json()['active_group_id']==previous
    denied=client.post('/v1/rbac/folders',headers={**as_('bob'),'X-FDP-Active-Group':owners},json={'name':'Denied','parent_id':parent['id']})
    assert denied.status_code==403
    assert infra.namespace_for(layers.PROD)!=infra.namespace_for(layers.NON_PROD)


def test_split_database_configuration_escapes_credentials_without_changing_legacy_url():
    from sqlalchemy.engine import make_url
    from app.config import Settings
    password="a@/:?#'password"
    settings=Settings(database_host='metadata-db',database_user='fdp',database_password=password)
    parsed=make_url(settings.database_url)
    assert parsed.password==password and parsed.host=='metadata-db' and parsed.database=='fdp'
    assert Settings(database_url='sqlite://').database_url=='sqlite://'


def test_failed_operation_can_retry_same_configuration_without_status_edits(client):
    path='/v1/admin/layers/'+layers.NON_PROD+'/infrastructure'
    client.put(path,headers=as_('admin'),json={'expected_generation':0,'spec':managed()}).raise_for_status()
    assert client.post(path+'/retry',headers=as_('admin'),json={'expected_generation':1}).status_code==409
    with SessionLocal() as db:
        operation=db.scalar(select(LayerOperation).where(LayerOperation.layer_id==layers.NON_PROD))
        operation.phase='Failed';db.commit()
    result=client.post(path+'/retry',headers=as_('admin'),json={'expected_generation':1})
    assert result.status_code==202,result.text
    assert result.json()['generation']==2 and result.json()['operation']['phase']=='Pending'
    assert client.post(path+'/retry',headers=as_('admin'),json={'expected_generation':1}).status_code==409


def test_execution_credentials_cannot_select_an_automation_primary_group(client):
    from app.auth import mint_token
    from app.ownership import GLOBAL_ADMIN
    ws=client.post('/v1/workspaces',headers=as_('admin'),json={
        'name':'Private','editor':'vscode','layer_id':layers.NON_PROD}).json()
    assert client.post('/v1/workspaces/'+ws['id']+'/start',headers=as_('admin')).status_code==200
    headers={'Authorization':'Bearer '+mint_token('execution','admin',60,ws=ws['id']),
             'X-FDP-Active-Group':GLOBAL_ADMIN}
    assert client.get('/v1/layers',headers=headers).status_code==403


def test_postgres_concurrent_infrastructure_updates_do_not_lose_a_generation(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from fastapi import HTTPException
    from app.db import engine
    if engine.dialect.name!='postgresql':
        pytest.skip('row-lock concurrency requires PostgreSQL')
    barrier=Barrier(2)
    def update():
        with SessionLocal() as db:
            from app.models import Resource
            cached_layer=db.get(Resource,layers.NON_PROD)
            assert cached_layer.config.get('infrastructure_generation',0)==0
            barrier.wait(timeout=5)
            try:
                result=infra.configure(db,layers.NON_PROD,infra.InfrastructureChange(
                    expected_generation=0,spec=infra.InfrastructureSpec.model_validate(managed())))
                db.commit()
                return result['generation']
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _:update(),range(2)))==[1,409]
