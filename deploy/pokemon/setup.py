#!/usr/bin/env python3
"""Idempotent local Pokémon project installation. Use the control-plane venv Python.

Creates only Pokemon-owned objects. Keeps the existing Gitea integration and platform admin.
Never writes data credentials into source or workspaces. Does not reset existing data.
"""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

import httpx

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
CA=Path(subprocess.check_output(['mkcert','-CAROOT'],text=True).strip())/'rootCA.pem'
BASE=os.environ.get('FDP_SERVER','https://localhost:8090')+'/api'
KC='https://localhost:8443'
SCIM=os.environ.get('FDP_SCIM_TOKEN','dev-only-scim-provisioning-token')
http=httpx.Client(verify=str(CA),timeout=130)
git_http=httpx.Client(base_url='http://localhost:3000/api/v1',auth=('fdp','fdp123'),timeout=60)


def command(args,**kwargs):
    return subprocess.run(args,check=True,**kwargs)


def prepare():
    envfile=HERE/'.env'
    if not envfile.exists():
        envfile.write_text(''.join(f'POKEMON_{env.upper()}_{purpose}_PASSWORD={secrets.token_urlsafe(28)}\n'
            for env in ('test','prod') for purpose in ('ADMIN','ENGINE')))
        envfile.chmod(0o600)
    values=dict(line.split('=',1) for line in envfile.read_text().splitlines() if line and not line.startswith('#'))
    for env in ('test','prod'):
        (HERE/'trino-catalog'/f'pokemon_{env}.properties').write_text(
            f'connector.name=postgresql\nconnection-url=jdbc:postgresql://pokemon-{env}-db:5432/pokemon\n'
            f'connection-user=pokemon_engine\nconnection-password={values[f"POKEMON_{env.upper()}_ENGINE_PASSWORD"]}\n'
            'postgresql.include-system-tables=false\n')
        (HERE/'trino-catalog'/f'pokemon_{env}.properties').chmod(0o600)
    return values


def compose(*args):
    return command(['docker','compose','--env-file',str(HERE/'.env'),'-f',str(ROOT/'deploy/compose/docker-compose.yml'),'-f',str(HERE/'docker-compose.yml'),*args],cwd=ROOT)


def login(username='admin',password='admin',realm='fdp',client='fdp-desktop'):
    response=http.post(f'{KC}/realms/{realm}/protocol/openid-connect/token',data={
        'grant_type':'password','client_id':client,'username':username,'password':password})
    response.raise_for_status();return response.json()['access_token']


def request(method,path,body=None,token=None):
    response=http.request(method,BASE+path,json=body,headers={'Authorization':'Bearer '+(token or ADMIN)})
    if response.is_error:
        raise RuntimeError(f'{method} {path}: {response.status_code} {response.text[:1000]}')
    return response.json() if response.content else None


def gitea(method,path,body=None):
    response=git_http.request(method,path,json=body)
    response.raise_for_status();return response.json() if response.content else None


def directory(collection,name,**attributes):
    key='userName' if collection=='Users' else 'displayName'
    items=request('GET','/scim/v2/'+collection,token=SCIM)['Resources']
    item=next((p for p in items if p[key]==name),None)
    body={key:name,**attributes}
    if item:
        return request('PUT',f"/scim/v2/{collection}/{item['id']}",body,token=SCIM)
    return request('POST','/scim/v2/'+collection,body,token=SCIM)


def folder(name,parent='workloads',**config):
    item=next((r for r in request('GET','/v1/rbac/resources') if r['name']==name and r['parent_id']==parent),None)
    return item or request('POST','/v1/rbac/folders',{'name':name,'parent_id':parent,**config})


def group(name,owner,bindings):
    item=next((g for g in request('GET','/v1/rbac/groups') if g['name']==name),None)
    if not item:
        item=request('POST','/v1/rbac/groups',{'name':name,'description':'Pokemon: environment-specific security group','owners':[owner]})
    for pid in bindings:
        request('POST',f"/v1/rbac/groups/{item['id']}/bindings",{'principal_id':pid})
    return item


def grant(resource,role,group=None,service=None,user=None):
    request('PUT',f"/v1/rbac/resources/{resource}/assignments",{'role':role,**({'app_group_id':group['id']} if group else {'principal_id':service or user})})


def identities():
    master=login(realm='master',client='admin-cli')
    headers={'Authorization':'Bearer '+master}
    users=http.get(KC+'/admin/realms/fdp/users',headers=headers,params={'username':'ana','exact':'true'}).json()
    if not users:
        response=http.post(KC+'/admin/realms/fdp/users',headers=headers,json={'username':'ana','enabled':True,'firstName':'Ana','lastName':'Pokemon Developer','email':'ana@fdp.local','emailVerified':True,'credentials':[{'type':'password','value':'ana','temporary':False}]})
        response.raise_for_status()
        users=http.get(KC+'/admin/realms/fdp/users',headers=headers,params={'username':'ana','exact':'true'}).json()
    uid=users[0]['id']
    existing=http.get(KC+'/admin/realms/fdp/groups',headers=headers).json()
    for name in ('pokemon-test-developers','pokemon-prod-dashboard-viewers'):
        item=next((g for g in existing if g['name']==name),None)
        if not item:
            response=http.post(KC+'/admin/realms/fdp/groups',headers=headers,json={'name':name});response.raise_for_status()
            item=next(g for g in http.get(KC+'/admin/realms/fdp/groups',headers=headers).json() if g['name']==name)
        response=http.put(f"{KC}/admin/realms/fdp/users/{uid}/groups/{item['id']}",headers=headers);response.raise_for_status()
    ana=directory('Users','ana',active=True,displayName='Ana · Pokemon Test developer')
    upstream=[directory('Groups',name,members=[{'value':ana['id']}]) for name in ('pokemon-test-developers','pokemon-prod-dashboard-viewers')]
    admin=next(p for p in request('GET','/v1/rbac/directory') if p['kind']=='user' and p['name']=='admin')
    return ana,upstream,admin


def repository():
    repos=gitea('GET','/user/repos')
    repo=next((r for r in repos if r['name']=='pokemon'),None)
    if not repo:
        repo=gitea('POST','/user/repos',{'name':'pokemon','private':True,'default_branch':'main','auto_init':False,'description':'Pokemon · Test/Prod TCGdex data product'})
        # Explicit paths, no local source-repo staging or credentials in the Git remote.
        work=HERE/'.checkout'
        work.mkdir(exist_ok=True)
        import shutil
        shutil.copytree(ROOT/'examples/pokemon',work,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
        for args in [['init','-b','main'],['config','user.name','Floren Data Platform'],['config','user.email','admin@fdp.local'],['add','.'],['commit','-m','Pokemon: governed TCGdex pipeline, SCD2 and dashboards'],['remote','add','origin','http://localhost:3000/fdp/pokemon.git']]:
            command(['git',*args],cwd=work,stdout=subprocess.DEVNULL)
        auth=base64.b64encode(b'fdp:fdp123').decode()
        command(['git','-c','http.extraHeader=Authorization: Basic '+auth,'push','-u','origin','main'],cwd=work,stdout=subprocess.DEVNULL)
        command(['git','-c','http.extraHeader=Authorization: Basic '+auth,'push','origin','main:refs/heads/test'],cwd=work,stdout=subprocess.DEVNULL)
    # Developer Git access is separate from OIDC. No technical integration credentials in workspaces.
    ana=next((u for u in gitea('GET','/admin/users') if u['login']=='ana'),None)
    if not ana:
        gitea('POST','/admin/users',{'username':'ana','email':'ana@fdp.local','password':'ana-pokemon-dev','must_change_password':False,'send_notify':False})
    gitea('PUT','/repos/fdp/pokemon/collaborators/ana',{'permission':'write'})
    protections=gitea('GET','/repos/fdp/pokemon/branch_protections')
    if not any(p.get('rule_name',p.get('branch_name'))=='main' for p in protections):
        gitea('POST','/repos/fdp/pokemon/branch_protections',{'branch_name':'main','rule_name':'main',
            'enable_push':True,'enable_push_whitelist':True,'push_whitelist_usernames':['fdp'],
            'enable_merge_whitelist':True,'merge_whitelist_usernames':['fdp'],
            'enable_status_check':False,'required_approvals':0,'block_on_rejected_reviews':True,
            'enable_force_push':False})
    return request('PUT','/v1/admin/repos/pokemon',{'name':'pokemon','url':'http://gitea:3000/fdp/pokemon.git','default_branch':'test',
        'description':'Pokemon · develop on test, admin-reviewed promotion to main','secret':{'username':'fdp','token':'fdp123'}})


def register_data(env,folder,owners,developers,values):
    name='pokemon-'+env
    source=request('PUT','/v1/admin/datasources/'+name,{'name':name,'kind':'postgres','description':f'Pokemon {env.title()} · isolated TCGdex warehouse',
        'config':{'host':name+'-db','port':5432,'database':'pokemon','catalog':'pokemon_'+env,'layer_id':folder['layer_id']},
        'secret':{'username':'pokemon_engine','password':values[f'POKEMON_{env.upper()}_ENGINE_PASSWORD']},
        'provisioner_secret':{'username':'pokemon_admin','password':values[f'POKEMON_{env.upper()}_ADMIN_PASSWORD']}})
    resources=request('GET','/v1/rbac/resources')
    parent=next(r for r in resources if r['kind']=='source' and r['object_id']==source['id'])
    grant(parent['id'],'Owner',group=owners)
    result={}
    for schema in (['raw','analytics','dbt_ana','dbt_admin'] if env=='test' else ['raw','analytics']):
        item=next((r for r in resources if r['parent_id']==parent['id'] and r['name']==schema),None)
        if not item:
            item=request('POST',f'/v1/rbac/sources/{name}/schemas',{'name':schema})
        request('POST',f"/v1/rbac/resources/{item['id']}/provision")
        result[schema]=item
    if env=='test':
        for schema in ('raw','analytics'):
            grant(result[schema]['id'],'Reader',group=developers)
        ana=next(p for p in request('GET','/v1/rbac/directory') if p['kind']=='user' and p['name']=='ana')
        grant(result['dbt_ana']['id'],'Admin',user=ana['id'])
        assignments=request('GET',f"/v1/rbac/resources/{result['dbt_ana']['id']}/assignments")
        if any(a.get('app_group_id')==developers['id'] and not a['inherited'] for a in assignments):
            request('DELETE',f"/v1/rbac/resources/{result['dbt_ana']['id']}/assignments/{developers['id']}")
    return result


def api(env,folder,owners,developers):
    slug='tcgdex-'+env
    item=next((a for a in request('GET','/v1/apis')['apis'] if a['slug']==slug),None)
    if not item:
        item=request('POST','/v1/apis',{'name':'Pokemon · TCGdex '+env.title(),'slug':slug,'direction':'external','protocol':'rest',
            'layer_id':folder['layer_id'],'description':'Pokemon: public English sets and cards; governed Consumer access',
            'outbound':{'base_url':'https://api.tcgdex.net/v2/en','timeout_seconds':60}})
        for name,path in [('set','/sets/{set_id}'),('card','/cards/{card_id}')]:
            request('POST',f"/v1/apis/{item['id']}/operations",{'name':name,'group':'English','method':'GET','path':path,'description':'Pokemon: read '+name})
    grant(item['resource_id'],'Owner',group=owners)
    if env=='test':grant(item['resource_id'],'Consumer',group=developers)
    return item


def workloads(env,folder,repo,schemas,api,owners,viewers):
    existing=request('GET','/v1/workloads')
    pubs={}
    for name in ('ingest','models','streamlit','shiny','pipelines'):
        slug=f'pokemon-{name}-{env}'
        pub=next((p for p in existing if p['slug']==slug),None)
        if not pub:
            pub=request('POST','/v1/workloads',{'repo_id':repo['id'],'folder_id':folder['id'],
                'branch':'test' if env=='test' else 'main','program':name+'-'+env,'slug':slug,
                'title':f'Pokemon · {name.title()} · {env.title()}','execution_mode':'fixed','warehouse':'xsmall','auto_update':True})
        pubs[name]=pub
        pid=pub['service_principal_id']
        if name=='ingest':
            grant(schemas['raw']['id'],'Contributor',service=pid)
            grant(api['resource_id'],'Consumer',service=pid)
        elif name=='models':
            grant(schemas['raw']['id'],'Reader',service=pid)
            grant(schemas['analytics']['id'],'Admin',service=pid)
        elif name in ('streamlit','shiny'):
            grant(schemas['analytics']['id'],'Reader',service=pid)
            if env=='prod':grant(pub['resource_id'],'Executor',group=viewers)
    for name in ('ingest','models'):
        grant(pubs[name]['resource_id'],'Executor',service=pubs['pipelines']['service_principal_id'])
    # Retry any initial deployment that raced identity assignment.
    for pub in pubs.values():request('POST',f"/v1/workloads/{pub['id']}/sync")
    return pubs


def workspaces(folder):
    import tempfile
    with tempfile.TemporaryDirectory(prefix='pokemon-git-') as tmp:
        bare=Path(tmp)/'repo.git'
        bundle=Path(tmp)/'pokemon.bundle'
        auth=base64.b64encode(b'fdp:fdp123').decode()
        command(['git','-c','http.extraHeader=Authorization: Basic '+auth,'clone','--bare',
                 'http://localhost:3000/fdp/pokemon.git',str(bare)],stdout=subprocess.DEVNULL)
        command(['git','-C',str(bare),'bundle','create',str(bundle),'--branches'],stdout=subprocess.DEVNULL)
        for who in ('ana','admin'):
            token=login(who,who)
            existing=request('GET','/v1/workspaces',token=token)
            ws=next((w for w in existing if w['name']=='Pokemon Test'),None)
            if not ws:ws=request('POST','/v1/workspaces',{'name':'Pokemon Test','editor':'vscode','layer_id':folder['id']},token=token)
            ws=request('POST',f"/v1/workspaces/{ws['id']}/start",token=token)
            # A real test-branch checkout, without any integration credentials in its
            # config/reflog/history. Preserve initialized checkouts on repeat installs.
            command(['docker','exec','-i','ws-'+ws['id'],'sh','-c',
                'cat > /tmp/pokemon.bundle; '
                'if ! git -C /workspace/pokemon rev-parse --verify HEAD >/dev/null 2>&1; then '
                'if [ -d /workspace/pokemon ]; then mv /workspace/pokemon /workspace/pokemon-before-git; fi; '
                'git clone --branch test /tmp/pokemon.bundle /workspace/pokemon && '
                'git -C /workspace/pokemon remote set-url origin http://gitea:3000/fdp/pokemon.git; fi; '
                'rm -f /tmp/pokemon.bundle'],input=bundle.read_bytes(),stdout=subprocess.DEVNULL)
            print(f'{who}: Test workspace {ws["id"]}')


def metadata(owners):
    assets=[]
    offset=0
    while True:
        page=request('GET',f'/v1/catalog/search?limit=100&offset={offset}&sort=name')
        assets.extend(page['items'])
        offset+=len(page['items'])
        if offset>=page['total'] or not page['items']:break
    for asset in assets:
        if 'pokemon' not in (asset.get('path','')+' '+asset.get('name','')).lower():
            continue
        env='Prod' if ('prod' in asset.get('path','').lower() or 'prod' in asset.get('name','').lower() or asset.get('attributes',{}).get('branch')=='main') else 'Test'
        request('PUT',f"/v1/catalog/assets/{asset['id']}/governance",{'description':asset.get('description') or 'Pokemon · TCGdex English card data product',
            'domain':'Pokemon','owner_group_id':owners['id'],'steward':'admin','classification':'public','tags':['Pokemon',env], 'status':'reviewed'})
        if asset['kind'] in ('script','dbt','table'):
            request('PUT',f"/v1/catalog/assets/{asset['id']}/sla",{'freshness_minutes':1560,'success_rate_percent':99 if asset['resource']['kind']=='service' else None,'window_hours':168})


def main():
    global ADMIN
    values=prepare()
    compose('up','-d','pokemon-test-db','pokemon-prod-db')
    for env in ('test','prod'):
        for attempt in range(60):
            ready=subprocess.run(['docker','exec',f'fdp-pokemon-{env}-db-1','pg_isready','-U','pokemon_admin','-d','pokemon'],stdout=subprocess.DEVNULL)
            if ready.returncode==0:break
            time.sleep(1)
        sql=(HERE/'schema.sql').read_text()+ ('\nCREATE SCHEMA IF NOT EXISTS dbt_ana;CREATE SCHEMA IF NOT EXISTS dbt_admin;\n' if env=='test' else '')
        command(['docker','exec','-i',f'fdp-pokemon-{env}-db-1','psql','-X','-v','ON_ERROR_STOP=1','-U','pokemon_admin','-d','pokemon'],input=sql,text=True,stdout=subprocess.DEVNULL)
    ADMIN=login()
    ana,upstream,admin=identities()
    owners=group('Pokemon Platform Owners',admin['id'],[admin['id']])
    developers=group('Pokemon Test Developers',admin['id'],[upstream[0]['id']])
    viewers=group('Pokemon Prod Dashboard Viewers',admin['id'],[upstream[1]['id']])
    project=folder('Pokemon')
    grant(project['id'],'Owner',group=owners)
    configured_layers={layer['name']:layer for layer in request('GET','/v1/layers')}
    folders={env:folder(env.title(),project['id'],environment=True,workspaces=env=='test',branch='test' if env=='test' else 'main',layer_id=configured_layers['Non-Prod' if env=='test' else 'Prod']['id']) for env in ('test','prod')}
    grant(folders['test']['id'],'Admin',group=developers)
    repo=repository()
    state={'folders':folders,'repo':repo,'workloads':{}}
    # Register/provision before Trino restart so its engine logins exist.
    for env in ('test','prod'):
        schemas=register_data(env,folders[env],owners,developers,values)
        external=api(env,folders[env],owners,developers)
        state.setdefault('schemas',{})[env]=schemas
        state.setdefault('apis',{})[env]=external
    compose('up','-d','--no-deps','--force-recreate','trino')
    # A running JVM can still reject queries with SERVER_STARTING_UP.
    for attempt in range(120):
        ready=subprocess.run(['docker','exec','fdp-control-plane-1','python','-c',
            'import httpx,sys; r=httpx.get("http://trino:8080/v1/info",timeout=3); '
            'sys.exit(0 if r.status_code==200 and not r.json().get("starting",True) else 1)'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if ready.returncode==0:break
        time.sleep(1)
    else:raise RuntimeError('Trino did not become ready')
    for env in ('test','prod'):
        state['workloads'][env]=workloads(env,folders[env],repo,state['schemas'][env],state['apis'][env],owners,viewers)
    (HERE/'state.json').write_text(json.dumps(state,indent=2))
    workspaces(configured_layers['Non-Prod'])
    metadata(owners)
    print('Pokemon configured. Run verify.py to execute pipelines, inspect lineage and prove isolation.')

if __name__=='__main__':main()
