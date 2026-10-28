from datetime import timedelta
from urllib.parse import urlsplit

import httpx

from app import deps, workspace_lifecycle as lifecycle
from app.config import get_settings
from app.db import SessionLocal
from app.models import Workspace, utcnow
from .conftest import as_


def create(client,editor='sql'):
    result=client.post('/v1/workspaces',headers=as_('admin'),json={'name':'Editor startup','editor':editor})
    assert result.status_code==201,result.text
    return result.json()['id']


def tick(ws_id):
    with SessionLocal() as db:
        lifecycle.refresh_workspace(db,deps.get_orchestrator(),db.get(Workspace,ws_id),get_settings())
        db.commit()


def test_start_waits_for_editor_readiness_prevents_duplicate_start_and_authenticates_loading(client,monkeypatch):
    orch=deps.get_orchestrator()
    monkeypatch.setattr(get_settings(),'orchestrator','docker')
    monkeypatch.setattr(lifecycle,'http_ready',lambda ws:False)
    ws_id=create(client)
    result=client.post('/v1/workspaces/'+ws_id+'/start',headers=as_('admin'))
    assert result.status_code==200,result.text
    assert result.json()['status']=='starting'
    ref='ws-'+ws_id
    spec=orch.executions[ref]
    assert client.post('/v1/workspaces/'+ws_id+'/start',headers=as_('admin')).json()['status']=='starting'
    assert orch.executions[ref] is spec
    assert client.get('/ws/'+ws_id+'/').status_code==401
    opened=client.post('/v1/workspaces/'+ws_id+'/open',headers=as_('admin')).json()['url']
    uri=urlsplit(opened)
    response=client.get('/internal/auth/forward',headers={'X-Forwarded-Uri':uri.path+'?'+uri.query},follow_redirects=False)
    cookie=response.cookies['dc_ws_'+ws_id]
    client.cookies.set('dc_ws_'+ws_id,cookie)
    loading=client.get('/ws/'+ws_id+'/')
    assert loading.status_code==503 and 'Starting your workspace' in loading.text
    assert 'http-equiv="refresh"' in loading.text and loading.headers['cache-control']=='no-store'
    stale_route=client.get('/internal/auth/forward',headers={'X-Forwarded-Uri':'/ws/'+ws_id+'/'})
    assert stale_route.status_code==503 and 'Starting your workspace' in stale_route.text
    tick(ws_id)
    assert client.get('/v1/workspaces',headers=as_('admin')).json()[0]['status']=='starting'
    monkeypatch.setattr(lifecycle,'http_ready',lambda ws:True)
    tick(ws_id)
    assert client.get('/v1/workspaces',headers=as_('admin')).json()[0]['status']=='running'
    # A separate identity cannot read another user's loading/failed editor page.
    client.cookies.clear()
    assert client.get('/ws/'+ws_id+'/').status_code==401


def test_failed_start_timeout_and_stop_retain_files_and_are_restartable(client,monkeypatch):
    orch=deps.get_orchestrator()
    monkeypatch.setattr(get_settings(),'orchestrator','docker')
    monkeypatch.setattr(lifecycle,'http_ready',lambda ws:False)
    ws_id=create(client)
    result=client.post('/v1/workspaces/'+ws_id+'/start',headers=as_('admin')).json()
    volume=orch.executions['ws-'+ws_id].home_volume
    orch.volumes[volume]={'/workspace/sql/keep.sql':b'SELECT 1'}
    orch.states['ws-'+ws_id]='failed'
    tick(ws_id)
    failed=client.get('/v1/workspaces',headers=as_('admin')).json()[0]
    assert failed['status']=='failed' and 'exited before' in failed['startup_error']
    assert client.post('/v1/workspaces/'+ws_id+'/open',headers=as_('admin')).status_code==409
    assert client.post('/v1/workspaces/'+ws_id+'/start',headers=as_('admin')).json()['status']=='starting'
    with SessionLocal() as db:
        db.get(Workspace,ws_id).started_at=utcnow()-timedelta(seconds=get_settings().workspace_start_timeout_seconds+1)
        db.commit()
    tick(ws_id)
    failed=client.get('/v1/workspaces',headers=as_('admin')).json()[0]
    assert failed['status']=='failed' and 'within' in failed['startup_error']
    assert 'ws-'+ws_id not in orch.executions
    assert orch.volumes[volume]['/workspace/sql/keep.sql']==b'SELECT 1'
    client.post('/v1/workspaces/'+ws_id+'/start',headers=as_('admin')).raise_for_status()
    assert client.post('/v1/workspaces/'+ws_id+'/stop',headers=as_('admin')).json()['status']=='stopped'
    tick(ws_id)
    assert client.get('/v1/workspaces',headers=as_('admin')).json()[0]['status']=='stopped'


def test_sql_readiness_checks_health_at_workspace_prefix_not_a_static_page(monkeypatch):
    ws=Workspace(id='a'*12,editor='sql',upstream_url='http://editor:8978')
    responses=[httpx.Response(200,json={'health':'starting'}),httpx.Response(200,text='<html>'),httpx.Response(503),httpx.Response(200,json={'health':'ok'})]
    calls=[]
    def get(url,**kwargs):
        calls.append(url)
        return responses.pop(0)
    monkeypatch.setattr(lifecycle.httpx,'get',get)
    assert [lifecycle.http_ready(ws) for _ in range(4)]==[False,False,False,True]
    assert set(calls)=={'http://editor:8978/ws/'+ws.id+'/status'}
