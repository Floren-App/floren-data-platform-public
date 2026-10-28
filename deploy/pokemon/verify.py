#!/usr/bin/env python3
"""Runtime acceptance: both pipelines, SQL results, IAM denials, gateway apps and lineage.

Run after setup.py. Does not weaken grants or impersonate workload identities.
"""
import json
from pathlib import Path
import subprocess
import time
import setup as s

s.ADMIN=s.login()
state=json.loads((s.HERE/'state.json').read_text())


def wait_live(pub_id,timeout=240):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        pub=s.request('GET','/v1/workloads/'+pub_id)
        if pub['status']=='live':return pub
        if pub['status']=='failed':raise RuntimeError(pub['slug']+': '+str(pub.get('sync_error')))
        time.sleep(2)
    raise RuntimeError('deployment timeout: '+pub_id)


def wait_run(run):
    deadline=time.monotonic()+1200
    while time.monotonic()<deadline:
        status=s.request('GET','/v1/runs/'+run['id'])
        if status['status'] not in ('pending','running'):
            logs=s.http.get(s.BASE+'/v1/runs/'+run['id']+'/logs',headers={'Authorization':'Bearer '+s.ADMIN}).text
            print(logs,flush=True)
            if status['status']!='succeeded':raise RuntimeError('run failed: '+run['id'])
            return status
        time.sleep(2)
    raise RuntimeError('run timed out: '+run['id'])


def execute(env,name,args=None):
    pub=wait_live(state['workloads'][env][name]['id'])
    return wait_run(s.request('POST',f"/v1/workloads/{pub['id']}/runs",{'args':args or {}}))


def sql(source,statement,token=None,parameters=None):
    return s.request('POST','/v1/sql/query',{'source':source,'sql':statement,'parameters':parameters or [],'max_rows':10000},token=token)


def denied(method,path,body=None,token=None):
    response=s.http.request(method,s.BASE+path,json=body,headers={'Authorization':'Bearer '+token})
    assert response.status_code in (401,403,404), (path,response.status_code,response.text)


def apps_and_access():
    ana=s.login('ana','ana')
    visible=s.request('GET','/v1/workloads',token=ana)
    assert {p['slug'] for p in visible if p['branch']=='main'}=={'pokemon-streamlit-prod','pokemon-shiny-prod'}
    for env in ('test','prod'):
        for name in ('streamlit','shiny'):
            pub=wait_live(state['workloads'][env][name]['id'])
            opened=s.request('POST',f"/v1/workloads/{pub['id']}/open",token=ana)
            response=s.http.get(opened['url'],follow_redirects=True)
            assert response.status_code==200,(pub['slug'],response.status_code,response.text[:150])
            print(pub['slug']+': gateway accessible to Ana',flush=True)
    denied('POST','/v1/sql/query',{'source':'pokemon-prod','sql':'SELECT * FROM analytics.cards_current'},token=ana)
    denied('POST','/v1/workspaces',{'name':'Prod forbidden','editor':'vscode','environment_id':state['folders']['prod']['id']},token=ana)
    for name in ('models','ingest','pipelines','streamlit','shiny'):
        pub=state['workloads']['prod'][name]
        denied('PATCH','/v1/workloads/'+pub['id'],{'env':{'ESCAPE':'1'}},token=ana)
    test_layer=next(r['layer_id'] for r in s.request('GET','/v1/rbac/resources') if r['id']==state['folders']['test']['id'])
    ws=next(w for w in s.request('GET','/v1/workspaces',token=ana) if w['layer_id']==test_layer)
    if ws['status']!='running':
        ws=s.request('POST',f"/v1/workspaces/{ws['id']}/start",token=ana)
    configured=s.request('GET',f"/v1/workspaces/{ws['id']}/config",token=ana)
    assert set(configured['config']['programs'])=={'pokemon/'+name+'-test' for name in ('ingest','models','streamlit','shiny','pipelines')}
    denied('POST','/v1/runs',{'workspace_id':ws['id'],'program':'pokemon/pipelines-prod'},token=ana)
    # Use the actual running workspace token inside its container; never export it to host output.
    probe='''import os,httpx,json
c=httpx.Client(base_url=os.environ['FDP_API'],headers={'Authorization':'Bearer '+os.environ['FDP_SESSION_TOKEN']},timeout=45)
prod=json.loads(os.environ['PROD_PROBE'])
checks=[('POST','/v1/sql/query',{'source':'pokemon-prod','sql':'SELECT * FROM analytics.cards_current'}),('GET','/v1/connect/pokemon-prod',None),('GET','/v1/egress/tcgdex-prod/sets/base1',None),('POST','/v1/orchestrate/runs',{'workload':'pokemon-ingest-prod'})]
for method,path,body in checks:
 r=c.request(method,path,json=body);assert r.status_code in (401,403,404),(path,r.status_code,r.text);print('Test workspace denied:',path,r.status_code)
r=c.post('/v1/sql/query',json={'source':'pokemon-test','sql':'SELECT count(*) FROM analytics.cards_current'});r.raise_for_status();assert r.json()['rows'][0][0]==166
# Direct HTTP bypass must fail too: Prod apps are on another Docker network.
for host in prod:
 try: httpx.get('http://'+host+':8501',timeout=3)
 except httpx.RequestError: print('Test network cannot reach:',host)
 else: raise AssertionError('cross-environment network reachable: '+host)
'''
    prod_hosts=[f"pub-{pub['id']}-{pub['deployed_sha'][:7]}" for pub in [wait_live(state['workloads']['prod'][name]['id']) for name in ('streamlit','shiny','pipelines')]]
    for host in list(prod_hosts):
        inspected=json.loads(subprocess.check_output(['docker','inspect',host],text=True))[0]
        prod_hosts.extend(n['IPAddress'] for n in inspected['NetworkSettings']['Networks'].values() if n.get('IPAddress'))
    s.command(['docker','exec','-e','PROD_PROBE='+json.dumps(prod_hosts),'ws-'+ws['id'],'python','-c',probe])
    print('Ana: Test developer, Prod dashboard viewer; direct Prod data/workloads denied',flush=True)


def main():
    for env in ('test','prod'):
        execute(env,'ingest')
        execute(env,'models',{'command':'build'})
        result=sql('pokemon-'+env,'SELECT (SELECT count(*) FROM analytics.cards_current),(SELECT count(*) FROM analytics.dim_cards_history),(SELECT count(*) FROM analytics.set_summary)')
        assert result['rows'][0][0]==166 and result['rows'][0][1]>=166 and result['rows'][0][2]==2,result
        print(env+f': 166 current cards, {result["rows"][0][1]} SCD2 versions, 2 sets',flush=True)
    apps_and_access()
    owners=next(g for g in s.request('GET','/v1/rbac/groups') if g['name']=='Pokemon Platform Owners')
    s.metadata(owners)
    print('Acceptance checks passed. Trigger DAGs separately to validate scheduled orchestration.',flush=True)

if __name__=='__main__':main()
