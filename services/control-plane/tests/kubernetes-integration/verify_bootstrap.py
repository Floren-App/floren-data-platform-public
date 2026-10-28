"""Exercise rendered PostgreSQL/Keycloak and real SCIM/OIDC on disposable Docker resources.

Run from the repository root with the control-plane virtualenv. This does not use
the current Kubernetes context, live Compose services, volumes, or platform DB.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[4]


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        # Docker arguments may contain disposable credentials. Do not include the
        # command line, environment, or provider response in a traceback.
        raise RuntimeError('Isolated bootstrap command failed: '+Path(args[0]).name)
    return result


def main():
    chart = ROOT/'deploy/helm/fdp'
    rendered = command(['helm','template','fdp',str(chart),'-n','fdp',
        '-f',str(chart/'examples/bundled.yaml'),'--set','keycloak.passwordGrantEnabled=true']).stdout
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    def resource(kind,name):
        return next(d for d in docs if d['kind']==kind and d['metadata']['name']==name)
    def secret(name,key):
        return base64.b64decode(resource('Secret',name)['data'][key]).decode()
    suffix = uuid.uuid4().hex[:10]
    network = 'dc-bootstrap-test-'+suffix
    pg, kc = 'dc-pg-test-'+suffix, 'dc-idp-test-'+suffix
    containers = []; network_created = False
    with tempfile.TemporaryDirectory(prefix='fdp-bootstrap-') as temporary:
        try:
            realm = Path(temporary)/'fdp-realm.json'
            realm.write_text(resource('ConfigMap','fdp-keycloak-realm')['data']['fdp-realm.json'])
            command(['docker','network','create',network]); network_created=True
            command(['docker','run','--rm','-d','--name',pg,'--network',network,
                '--network-alias','fdp-metadata-db','--tmpfs','/var/lib/postgresql/data',
                '-e','POSTGRES_PASSWORD='+secret('fdp-metadata-db','postgres-password'),'postgres:16-alpine'])
            containers.append(pg)
            for _ in range(80):
                if subprocess.run(['docker','exec',pg,'pg_isready','-U','postgres'],capture_output=True).returncode==0:break
                time.sleep(.5)
            else:raise RuntimeError('Isolated PostgreSQL did not become ready')
            bootstrap = resource('ConfigMap','fdp-metadata-bootstrap')['data']['init.sql']
            for _ in range(2):
                command(['docker','exec','-i','-e','APP_PASSWORD='+secret('fdp-metadata-db','fdp-password'),
                    '-e','KEYCLOAK_PASSWORD='+secret('fdp-metadata-db','keycloak-password'),pg,
                    'psql','-X','-U','postgres','-d','postgres','-v','ON_ERROR_STOP=1'],input=bootstrap)
            for user,database in [('fdp','keycloak'),('keycloak','fdp')]:
                forbidden=subprocess.run(['docker','exec',pg,'psql','-X','-U',user,'-d',database,'-c','SELECT 1'],capture_output=True)
                assert forbidden.returncode!=0,'Bundled database roles must not connect to each other\'s database'
            pod = resource('Deployment','fdp-keycloak')['spec']['template']['spec']['containers'][0]
            args = ['docker','run','--rm','-d','--name',kc,'--network',network,'-p','127.0.0.1::8080',
                    '-v',str(realm)+':/opt/keycloak/data/import/fdp-realm.json:ro']
            for entry in pod['env']:
                ref = entry.get('valueFrom',{}).get('secretKeyRef')
                value = secret(ref['name'],ref['key']) if ref else entry['value']
                args += ['-e',entry['name']+'='+value]
            command(args+[pod['image'],*pod['args']]);containers.append(kc)
            port = command(['docker','port',kc,'8080/tcp']).stdout.strip().rsplit(':',1)[1]
            endpoint = 'http://127.0.0.1:'+port
            with httpx.Client(base_url=endpoint,timeout=15) as identity:
                for _ in range(240):
                    try:
                        response=identity.get('/realms/fdp/protocol/openid-connect/certs')
                        if response.status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.5)
                else:raise RuntimeError('Isolated Keycloak did not become ready')
                auth=identity.post('/realms/master/protocol/openid-connect/token',data={
                    'grant_type':'password','client_id':'admin-cli','username':'bootstrap-admin',
                    'password':secret('fdp-keycloak','admin-password')})
                assert auth.status_code==200,'Keycloak bootstrap administrator authentication failed'
                identity.headers['Authorization']='Bearer '+auth.json()['access_token']
                base='/admin/realms/fdp'
                admin=identity.get(base+'/users',params={'username':'admin','exact':'true'}).json()[0]
                # Simulate the first-login profile/password enrollment. Password
                # grant is enabled only in this disposable test's rendered values.
                assert identity.put(base+'/users/'+admin['id'],json={'firstName':'Platform',
                    'lastName':'Administrator','email':'admin@example.invalid',
                    'emailVerified':True,'requiredActions':[]}).status_code==204
                response=identity.put(base+'/users/'+admin['id']+'/reset-password',json={
                    'type':'password','temporary':False,'value':secret('fdp-keycloak','initial-user-password')})
                assert response.status_code==204
                response=identity.post(base+'/users',json={'username':'ana','enabled':True})
                assert response.status_code==201
                ana_id=response.headers['Location'].rsplit('/',1)[1]
                response=identity.post(base+'/groups',json={'name':'Finance'})
                assert response.status_code==201
                finance_id=response.headers['Location'].rsplit('/',1)[1]
                response=identity.post(base+'/groups/'+finance_id+'/children',json={'name':'Readers'})
                assert response.status_code==201
                readers_id=response.headers['Location'].rsplit('/',1)[1]
                assert identity.put(base+'/users/'+ana_id+'/groups/'+readers_id).status_code==204
                provision=identity.post('/realms/fdp/protocol/openid-connect/token',data={
                    'grant_type':'client_credentials','client_id':'fdp-provisioner',
                    'client_secret':secret('fdp-keycloak','provisioner-client-secret')})
                assert provision.status_code==200,'Scoped provisioning client could not authenticate'
                # The adapter gets its own read-only IdP token, never the bootstrap administrator token.
                with httpx.Client(base_url=endpoint,timeout=30,
                    headers={'Authorization':'Bearer '+provision.json()['access_token']}) as reader:
                    os.environ.update(FDP_DATABASE_URL='sqlite://',FDP_ORCHESTRATOR='fake',
                        FDP_SECRETS_BACKEND='memory',FDP_DIRECTORY_MODE='scim',
                        FDP_SCIM_TOKEN=secret('fdp-control-plane','FDP_SCIM_TOKEN'),
                        FDP_OIDC_ISSUER='https://idp.example.com/realms/fdp',
                        FDP_OIDC_JWKS_URL=endpoint+'/realms/fdp/protocol/openid-connect/certs',
                        FDP_METADATA_SQL_ENABLED='false',
                        FDP_DATA_DIR=temporary+'/artifacts',FDP_GIT_CACHE_DIR=temporary+'/git')
                    # Ignore connection fields inherited from a developer's shell.
                    os.environ.pop('FDP_DATABASE_HOST',None)
                    sys.path.insert(0,str(ROOT/'services/control-plane'))
                    from fastapi.testclient import TestClient
                    from sqlalchemy import select,func
                    from app.main import app
                    from app.directory_sync import synchronize
                    from app.db import SessionLocal
                    from app import rbac
                    from app.models import DirectoryPrincipal, AuditEvent
                    with TestClient(app) as control:
                        control.headers['Authorization']='Bearer '+os.environ['FDP_SCIM_TOKEN']
                        result=synchronize(reader,control,'fdp')
                        assert result=={'users':2,'groups':3},result
                        with SessionLocal() as db:
                            before=db.scalar(select(func.count()).select_from(AuditEvent))
                            ana=db.scalar(select(DirectoryPrincipal).where(DirectoryPrincipal.kind=='user',DirectoryPrincipal.name=='ana'))
                            finance=db.scalar(select(DirectoryPrincipal).where(DirectoryPrincipal.kind=='group',DirectoryPrincipal.name=='/Finance'))
                            assert finance.id in rbac.closure(db,ana.id)
                        synchronize(reader,control,'fdp')
                        with SessionLocal() as db:
                            assert db.scalar(select(func.count()).select_from(AuditEvent))==before,'Repeated synchronization rewrote identities'
                        login=identity.post('/realms/fdp/protocol/openid-connect/token',data={
                            'grant_type':'password','client_id':'fdp-desktop','username':'admin',
                            'password':secret('fdp-keycloak','initial-user-password')})
                        assert login.status_code==200,'Initial application admin could not sign in'
                        response=control.get('/v1/me',headers={'Authorization':'Bearer '+login.json()['access_token']})
                        assert response.status_code==200,'Real OIDC token did not match the SCIM identity'
                        assert response.json()['is_admin'] is True
                        assert identity.put(base+'/users/'+ana_id,json={'enabled':False}).status_code==204
                        synchronize(reader,control,'fdp')
                        with SessionLocal() as db:
                            assert not db.scalar(select(DirectoryPrincipal).where(DirectoryPrincipal.kind=='user',DirectoryPrincipal.name=='ana')).active
            print('PASS: rendered database bootstrap and rerun; Keycloak realm/secret import; scoped IdP reader; SCIM users/nested groups; idempotency; real OIDC admin login; disabled-user propagation',flush=True)
        finally:
            for container in reversed(containers):
                subprocess.run(['docker','rm','-f','-v',container],capture_output=True)
            if network_created:subprocess.run(['docker','network','rm',network],capture_output=True)


if __name__=='__main__':
    main()
