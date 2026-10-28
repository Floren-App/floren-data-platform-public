"""Optional PostgreSQL TLS, retention and CNI boundaries on disposable k3s.

Uses a private kubeconfig and generated resources, never the active cluster or
Compose data. A Python pod represents the engine network identity; catalog/Trino
credential automation is deliberately not claimed by this service-level test.
"""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[4]
os.environ['FDP_DATABASE_URL'] = 'sqlite://'
os.environ['FDP_DATABASE_HOST'] = ''
sys.path.insert(0, str(ROOT/'services/control-plane'))
from app import layers
from app.layer_infrastructure import InfrastructureSpec, namespace_for
from app.layer_manifests import ControllerConfig, WorkerPlan, manifests
from tests.certificate_helpers import certificate_pair

IMAGE = os.environ.get('FDP_NETWORK_TEST_SERVER_IMAGE', 'fdp/control-plane:k3d-test')

PROBE = '''import json,os,socket,ssl,struct,sys
import psycopg
from app.layer_controller import KubernetesResources
host=os.environ['PGHOST']
password=os.environ['PG_ENGINE_PASSWORD']
options=dict(host=host,dbname='fdp',user='fdp_engine',password=password,
             sslmode='verify-full',sslrootcert='/fixture/ca.pem',connect_timeout=4,autocommit=True)
mode=sys.argv[1]
if mode=='denied':
 try:
  with socket.create_connection((host,5432),timeout=3): pass
 except OSError: print('DENIED'); sys.exit(0)
 raise AssertionError('Cross-layer/execution connection unexpectedly allowed')
if mode=='serial':
 with socket.create_connection((host,5432),timeout=4) as raw:
  raw.sendall(struct.pack('!II',8,80877103)); assert raw.recv(1)==b'S'
  with ssl.create_default_context(cafile='/fixture/ca.pem').wrap_socket(raw,server_hostname=host) as secured:
   print(int(secured.getpeercert()['serialNumber'],16)); sys.exit(0)
assert KubernetesResources(api=object()).verify_postgres(host), 'Controller must verify native TLS'
with psycopg.connect(**options) as connection:
 row=connection.execute("SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication FROM pg_roles WHERE rolname=current_user").fetchone()
 assert row==(False,False,False,False), 'Engine login must not be a provisioning administrator'
 if mode=='write':
  connection.execute('CREATE TABLE public.retained(value integer)')
  connection.execute('INSERT INTO public.retained VALUES (42)')
 assert connection.execute('SELECT value FROM public.retained').fetchall()==[(42,)]
 try: connection.execute('CREATE ROLE forbidden_role')
 except psycopg.errors.InsufficientPrivilege: pass
 else: raise AssertionError('Engine login must not create roles')
for change in ({'sslmode':'disable'},{'host':'wrong.invalid','hostaddr':socket.gethostbyname(host)},
               {'dbname':'postgres'},{'user':'postgres'}):
 try:
  with psycopg.connect(**{**options,**change}): pass
 except psycopg.Error: pass
 else: raise AssertionError('Rejected native connection unexpectedly succeeded')
print('PASS')
'''


def command(args, **kwargs):
    diagnostics = kwargs.pop('diagnostics', False)
    result = subprocess.run(args, capture_output=True, text=True, timeout=kwargs.pop('timeout', 120), **kwargs)
    if result.returncode:
        raise RuntimeError('Disposable layer PostgreSQL fixture failed: '+Path(args[0]).name+
                           (': '+result.stderr[-1800:] if diagnostics else ''))
    return result.stdout.strip()


def main():
    cluster = 'dc-pg-'+uuid.uuid4().hex[:8]
    created = False
    with tempfile.TemporaryDirectory(prefix='fdp-postgres-') as temporary:
        kubeconfig = str(Path(temporary)/'kubeconfig')
        env = {**os.environ, 'KUBECONFIG': kubeconfig}

        def kube(*args, **kwargs):
            return command(['kubectl', '--kubeconfig', kubeconfig, *args], env=env, **kwargs)

        def apply(items):
            kube('apply', '--server-side', '--field-manager=fdp-postgres-test', '-f', '-',
                 input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}))

        def wait(predicate, label, seconds=180):
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline:
                if predicate(): return
                time.sleep(2)
            raise AssertionError('Timed out waiting for '+label)

        try:
            print('Creating disposable PostgreSQL verification cluster', flush=True)
            command(['k3d', 'cluster', 'create', cluster, '--servers', '1', '--agents', '0', '--no-lb',
                '--image', 'rancher/k3s:v1.33.6-k3s1', '--kubeconfig-update-default=false',
                '--kubeconfig-switch-context=false', '--k3s-arg', '--disable=traefik@server:0',
                '--k3s-arg', '--disable=servicelb@server:0', '--wait'], env=env, timeout=180)
            created = True
            Path(kubeconfig).write_text(command(['k3d', 'kubeconfig', 'get', cluster], env=env))
            config = ControllerConfig('platform', 'pg-test', 'dc-pg-layer', 'runtime', artifact_image=IMAGE)
            spec = InfrastructureSpec(enabled=True, postgres={'enabled': True, 'credentials_secret': 'postgres-auth',
                'size': '1Gi', 'tls': {'mode': 'existing', 'secret_name': 'postgres-tls'}})
            command(['k3d', 'image', 'import', '-c', cluster, IMAGE, spec.postgres.image], env=env, timeout=180)
            docs = manifests(layers.NON_PROD, 1, spec, config, workers=WorkerPlan('test', 0.5, 1, 1))
            namespace = namespace_for(layers.NON_PROD, config.namespace_prefix)
            prod = namespace_for(layers.PROD, config.namespace_prefix)
            host = 'fdp-postgres.'+namespace+'.svc'
            key, cert, ca = certificate_pair(host)

            def certificate(key_bytes, certificate_bytes):
                apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':'postgres-tls','namespace':namespace},
                        'stringData':{'tls.key':key_bytes.decode(),'tls.crt':certificate_bytes.decode()}}])

            # Apply all generated boundaries, budgets and database resources. No
            # engine/artifact pods are needed for these service-level assertions.
            selected = [d for d in docs if d['kind'] in ('Namespace','NetworkPolicy','ResourceQuota','LimitRange','ServiceAccount')
                        or d['metadata']['name']=='fdp-postgres']
            apply([selected[0], {'apiVersion':'v1','kind':'Namespace','metadata':{'name':prod}}])
            password = uuid.uuid4().hex
            apply([{'apiVersion':'v1','kind':'Secret','metadata':{'name':'postgres-auth','namespace':namespace},
                    'stringData':{'postgres-password':uuid.uuid4().hex,'engine-password':password}},
                   {'apiVersion':'v1','kind':'Secret','metadata':{'name':'engine-auth','namespace':namespace},
                    'stringData':{'password':password}}])
            certificate(key, cert)
            apply(selected[1:])
            archive = io.BytesIO()
            with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as zipped:
                for path in sorted((ROOT/'services/control-plane/app').rglob('*.py')):
                    zipped.writestr('app/'+path.relative_to(ROOT/'services/control-plane/app').as_posix(),path.read_bytes())
            data = {'probe.py':PROBE,'ca.pem':ca[1].public_bytes(serialization.Encoding.PEM).decode()}
            apply([{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'fixture','namespace':namespace},
                    'data':data,'binaryData':{'app.zip':base64.b64encode(archive.getvalue()).decode()}}])

            def pod(name, ns, labels):
                return {'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':ns,'labels':labels},
                    'spec':{'automountServiceAccountToken':False,'containers':[{'name':'probe','image':IMAGE,
                        'workingDir':'/tmp','command':['sleep','infinity'],
                        'env':[{'name':'PGHOST','value':host}, {'name':'PYTHONPATH','value':'/fixture/app.zip'},
                               {'name':'SSL_CERT_FILE','value':'/fixture/ca.pem'},
                               *([{'name':'PG_ENGINE_PASSWORD','valueFrom':{'secretKeyRef':{'name':'engine-auth','key':'password'}}}] if ns==namespace else [])],
                        **({'volumeMounts':[{'name':'fixture','mountPath':'/fixture','readOnly':True}]} if ns==namespace else {})}],
                        **({'volumes':[{'name':'fixture','configMap':{'name':'fixture'}}]} if ns==namespace else {})}}

            apply([pod('engine',namespace,{'app.kubernetes.io/component':'trino','fdp.floren.app/layer':layers.NON_PROD}),
                   pod('app',namespace,{'fdp.floren.app/managed':'true','fdp.floren.app/layer':layers.NON_PROD}),
                   pod('foreign',prod,{'app.kubernetes.io/component':'trino','fdp.floren.app/layer':layers.NON_PROD})])
            # Open only the negative probes' egress, so their denied connections
            # prove database ingress isolation independently of source-side denial.
            for ns, selector in [(namespace,{'fdp.floren.app/managed':'true'}),(prod,{})]:
                apply([{'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy',
                    'metadata':{'name':'fixture-egress','namespace':ns},
                    'spec':{'podSelector':{'matchLabels':selector},'policyTypes':['Egress'],'egress':[{}]}}])
            print('Waiting for generated database, bootstrap role and TLS reload sidecar',flush=True)
            kube('rollout','status','deployment/fdp-postgres','-n',namespace,'--timeout=180s',timeout=210,diagnostics=True)
            for name, ns in [('engine',namespace),('app',namespace),('foreign',prod)]:
                kube('wait','pod/'+name,'-n',ns,'--for=condition=Ready','--timeout=120s')

            def probe(mode):
                return kube('exec','-n',namespace,'engine','--','python','/fixture/probe.py',mode,diagnostics=True)

            assert probe('write')=='PASS'
            # Copy only the public probe script into the foreign pod; no secrets.
            denial = "import socket,sys\ntry: socket.create_connection((sys.argv[1],5432),timeout=3)\nexcept OSError: print('DENIED');sys.exit(0)\nraise AssertionError('Cross-layer/execution access allowed')"
            for name, ns in [('app',namespace),('foreign',prod)]:
                assert kube('exec','-n',ns,name,'--','python','-c',denial,host,diagnostics=True)=='DENIED'
            print('PASS: native verified TLS, restricted engine role, plaintext/wrong-host/admin denial, app and cross-layer denial',flush=True)
            pods=json.loads(kube('get','pods','-n',namespace,'-l','app.kubernetes.io/component=postgres','-o','json'))['items']
            original=pods[0]
            identity=[(c['name'],c['containerID'],c['restartCount']) for c in original['status']['containerStatuses']]
            key2,cert2,_=certificate_pair(host,ca=ca)
            certificate(key2,cert2)
            from cryptography import x509
            serial=str(x509.load_pem_x509_certificate(cert2).serial_number)
            wait(lambda:probe('serial')==serial,'native PostgreSQL certificate reload')
            current=json.loads(kube('get','pod',original['metadata']['name'],'-n',namespace,'-o','json'))
            assert identity==[(c['name'],c['containerID'],c['restartCount']) for c in current['status']['containerStatuses']]
            assert probe('read')=='PASS'
            print('PASS: projected TLS renewal reloads PostgreSQL without a container restart',flush=True)
            kube('delete','pod',original['metadata']['name'],'-n',namespace,'--wait=true')
            kube('rollout','status','deployment/fdp-postgres','-n',namespace,'--timeout=180s',timeout=210)
            assert probe('read')=='PASS'
            print('PASS: replacement database pod preserves the existing PVC data and engine login',flush=True)
        finally:
            if created or subprocess.run(['docker','inspect','k3d-'+cluster+'-server-0'],capture_output=True).returncode==0:
                subprocess.run(['k3d','cluster','delete',cluster],env=env,capture_output=True,timeout=120)


if __name__=='__main__':
    main()
