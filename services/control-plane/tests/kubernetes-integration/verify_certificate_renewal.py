"""Actual cert-manager -> projected Secret -> TLS sidecar -> Trino renewal.

Uses only a disposable k3d cluster and private kubeconfig. Current Python sources
are mounted as a zip over a cached dependency image, avoiding a large rebuild.
The fixture policy server is allow-all: this verifies certificates, not RBAC.
"""
import base64
from datetime import datetime, timezone
import hashlib
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

import httpx
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[4]
os.environ['FDP_DATABASE_URL'] = 'sqlite://'
os.environ.pop('FDP_DATABASE_HOST', None)
sys.path.insert(0, str(ROOT/'services/control-plane'))
from app import layers
from app.layer_infrastructure import InfrastructureSpec, namespace_for
from app.layer_manifests import ControllerConfig, WorkerPlan, manifests

IMAGE = os.environ.get('FDP_NETWORK_TEST_SERVER_IMAGE', 'fdp/control-plane:k3d-test')
LOCK = json.loads(Path(__file__).with_name('cert-manager.lock.json').read_text())

POLICY = '''from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from app.sql_access import layer_jwks
import app.sql_access
assert app.sql_access.__file__.startswith('/test-source/app.zip/'), 'Fixture must use current sources'
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def reply(self,body):
  data=json.dumps(body).encode(); self.send_response(200)
  self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(data)
 def do_GET(self): self.reply(layer_jwks())
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  action=body.get('input',{}).get('action',{})
  result=list(range(len(action['filterResources']))) if 'filterResources' in action else ([] if self.path.endswith('row-filters') else True)
  self.reply({'result':result})
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''

# The platform's own certificate monitoring, run against the live Certificate and endpoint.
MONITOR = '''import json,sys
from datetime import datetime,timezone
from app import cert_monitor as m
status,host=json.loads(sys.argv[1]),sys.argv[2]
served=m.served_certificate(host,8443)
managed=m.manager_status(status)
state,message=m.classify(datetime.now(timezone.utc),served.not_valid_after_utc if served else None,
                         managed['renewal_time'],managed['ready'],managed['failure'])
print(json.dumps({'state':state,'message':message,'serial':str(served.serial_number) if served else None,
                  'renewal_time':managed['renewal_time'].isoformat() if managed['renewal_time'] else None}))
'''

ICEBERG = '''import asyncio,json,os,sys
from types import SimpleNamespace
from app import sql_access as sql
layer=os.environ['TEST_LAYER']
async def run(statement):
 q=SimpleNamespace(id='iceberg-test',principal_id='fixture-user',engine_layer=layer,engine_scheme='https')
 headers=sql.engine_headers(q)
 try:
  response=await sql.engine_request('POST','/v1/statement',headers=headers,query=q,content=statement.encode())
  body=response.json(); rows=list(body.get('data',[]))
  for _ in range(200):
   if body.get('error'): print(json.dumps({'error':body['error'].get('message','')[:300]})); return
   if not body.get('nextUri'): break
   response=await sql.engine_request('GET',sql.engine_path(body['nextUri']),headers=headers,query=q)
   body=response.json(); rows.extend(body.get('data',[])); await asyncio.sleep(.1)
  print(json.dumps({'rows':rows}))
 finally: await sql.close_engine_client()
asyncio.run(run(sys.argv[1]))
'''

PROBE = '''import asyncio,json,os,socket,ssl
from types import SimpleNamespace
from cryptography import x509
from app import sql_access as sql
assert sql.__file__.startswith('/test-source/app.zip/'), 'Probe must use current sources'
from app.layer_controller import KubernetesResources
from app.layer_infrastructure import namespace_for
from app.config import get_settings
layer=os.environ['TEST_LAYER']
hostname='fdp-trino.'+namespace_for(layer,get_settings().layer_namespace_prefix)+'.svc'
async def run():
 if not KubernetesResources(api=object()).verify_https(hostname):
  print(json.dumps({'ready':False})); return
 q=SimpleNamespace(id='certificate-test',principal_id='fixture-user',engine_layer=layer,engine_scheme='https')
 headers=sql.engine_headers(q)
 try:
  response=await sql.engine_request('POST','/v1/statement',headers=headers,query=q,content=b'SELECT count(*) FROM tpch.tiny.nation')
  assert response.status_code==200, 'SQL status '+str(response.status_code)
  body=response.json(); rows=list(body.get('data',[]))
  for _ in range(100):
   assert not body.get('error'), 'Authenticated query failed: '+str((body.get('error') or {}).get('errorName'))
   if not body.get('nextUri'): break
   response=await sql.engine_request('GET',sql.engine_path(body['nextUri']),headers=headers,query=q)
   assert response.status_code==200, 'SQL page status '+str(response.status_code)
   body=response.json(); rows.extend(body.get('data',[])); await asyncio.sleep(.05)
  assert rows==[[25]], 'Worker query did not complete'
  context=ssl.create_default_context(cafile=os.environ['SSL_CERT_FILE'])
  with socket.create_connection((hostname,8443),timeout=5) as raw:
   with context.wrap_socket(raw,server_hostname=hostname) as secured:
    serial=x509.load_der_x509_certificate(secured.getpeercert(binary_form=True)).serial_number
  print(json.dumps({'ready':True,'rows':rows,'serial':str(serial)}))
 finally: await sql.close_engine_client()
asyncio.run(run())
'''


def command(args, **kwargs):
    diagnostics = kwargs.pop('diagnostics', False)
    result = subprocess.run(args, capture_output=True, text=True, timeout=kwargs.pop('timeout', 120), **kwargs)
    if result.returncode:
        # Some commands submit Secrets. Never include stdin, full command lines,
        # kubectl response bodies or private key data in failure reports.
        raise RuntimeError('Disposable certificate fixture command failed: ' + Path(args[0]).name +
                           (': ' + result.stderr[-3000:] if diagnostics else ''))
    return result.stdout


def source_zip():
    stream = io.BytesIO()
    app = ROOT/'services/control-plane/app'
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(app.rglob('*.py')):
            archive.writestr('app/' + path.relative_to(app).as_posix(), path.read_bytes())
    return base64.b64encode(stream.getvalue()).decode()


def main():
    suffix = uuid.uuid4().hex[:8]
    cluster = 'dc-cert-' + suffix
    created = False
    with tempfile.TemporaryDirectory(prefix='fdp-certificates-') as temporary:
        kubeconfig = str(Path(temporary)/'kubeconfig')
        env = {**os.environ, 'KUBECONFIG': kubeconfig}

        def kube(*args, **kwargs):
            return command(['kubectl', '--kubeconfig', kubeconfig, *args], env=env, **kwargs)

        def apply(items):
            kube('apply', '--server-side', '--field-manager=fdp-certificate-verification', '-f', '-',
                 input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}),
                 diagnostics=all(item['kind'] != 'Secret' for item in items))

        def resource(kind, name, namespace):
            return json.loads(kube('get', kind, name, '-n', namespace, '-o', 'json'))

        def wait(predicate, label, seconds=180):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                result = predicate()
                if result:
                    return result
                time.sleep(2)
            raise AssertionError('Timed out waiting for ' + label)

        try:
            cached = os.environ.get('FDP_CERT_MANAGER_MANIFEST')
            if cached:
                manifest = Path(cached).read_bytes()
            else:
                manifest = httpx.get(LOCK['url'], follow_redirects=True, timeout=60).raise_for_status().content
            assert hashlib.sha256(manifest).hexdigest() == LOCK['sha256'], 'cert-manager manifest checksum mismatch'
            cert_docs = [doc for doc in yaml.safe_load_all(manifest) if doc]
            images = sorted({c['image'] for d in cert_docs if d['kind'] == 'Deployment'
                             for c in d['spec']['template']['spec']['containers']})
            for image in images:
                if subprocess.run(['docker', 'image', 'inspect', image], capture_output=True).returncode:
                    command(['docker', 'pull', image], timeout=180)
            print('Creating private k3d cluster with pinned cert-manager ' + LOCK['version'], flush=True)
            command(['k3d', 'cluster', 'create', cluster, '--servers', '1', '--agents', '0', '--no-lb',
                '--image', 'rancher/k3s:v1.33.6-k3s1', '--kubeconfig-update-default=false',
                '--kubeconfig-switch-context=false', '--k3s-arg', '--disable=traefik@server:0',
                '--k3s-arg', '--disable=servicelb@server:0', '--wait'], env=env, timeout=180)
            created = True
            Path(kubeconfig).write_text(command(['k3d', 'kubeconfig', 'get', cluster], env=env))
            print('Importing cached runtime and cert-manager images', flush=True)
            command(['k3d', 'image', 'import', '-c', cluster, IMAGE, 'trinodb/trino:484', 'postgres:16.15-bookworm',
                     'rustfs/rustfs:1.0.2', *images], env=env, timeout=300)
            # Server-side apply avoids the CRD client-side annotation size limit.
            print('Installing cert-manager CRDs and controllers', flush=True)
            kube('apply', '--server-side', '-f', '-', input=manifest.decode())
            for name in ('cert-manager', 'cert-manager-webhook', 'cert-manager-cainjector'):
                kube('rollout', 'status', 'deployment/'+name, '-n', 'cert-manager', '--timeout=120s')

            config = ControllerConfig('platform', 'certificate-test', 'dc-cert-layer', 'dc-cert-runtime', artifact_image=IMAGE)
            spec = InfrastructureSpec(enabled=True, trino={'coordinator': {'cpu': .5, 'memory': '1Gi'},
                'catalog_secret': 'fixture-catalogs',
                'tls': {'mode': 'cert_manager', 'secret_name': 'trino-tls', 'duration_hours': 24,
                        'renew_before_hours': 1, 'issuer': {'name': 'test-ca'},
                        # Java trust for Trino's S3 client to RustFS over HTTPS (the issuing CA).
                        'trust_bundle': {'name': 'java-trust-v1', 'key': 'ca.pem'}}},
                storage={'artifact_size': '1Gi', 'artifact_access_mode': 'ReadWriteOnce'},
                # Iceberg on layer storage with RustFS over cert-manager TLS.
                postgres={'enabled': True, 'cpu': .25, 'memory': '256Mi', 'size': '1Gi'},
                rustfs={'enabled': True, 'image': 'rustfs/rustfs:1.0.2', 'cpu': .25, 'memory': '384Mi', 'size': '1Gi',
                        'tls': {'mode': 'cert_manager', 'secret_name': 'rustfs-tls', 'duration_hours': 24,
                                'renew_before_hours': 1, 'issuer': {'name': 'test-ca'}}},
                iceberg={'enabled': True, 'catalog': 'lake', 'bucket': 'lake'})
            workers = WorkerPlan('test', 0.5, 1, 1)  # one worker on a 1 GB warehouse
            docs = manifests(layers.NON_PROD, 1, spec, config, workers=workers)
            namespace = namespace_for(layers.NON_PROD, config.namespace_prefix)
            apply([docs[0], {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'platform'}},
                   {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole',
                    'metadata': {'name': config.runtime_cluster_role}, 'rules': []}])

            print('Issuing disposable CA and layer certificate', flush=True)
            apply([{'apiVersion': 'cert-manager.io/v1', 'kind': 'Issuer',
                    'metadata': {'name': 'selfsigned', 'namespace': namespace}, 'spec': {'selfSigned': {}}},
                   {'apiVersion': 'cert-manager.io/v1', 'kind': 'Certificate',
                    'metadata': {'name': 'test-root', 'namespace': namespace},
                    'spec': {'isCA': True, 'commonName': 'Disposable Floren Data Platform CA', 'secretName': 'test-root',
                             'duration': '168h', 'issuerRef': {'name': 'selfsigned'}}}])
            kube('wait', '-n', namespace, '--for=condition=Ready', 'certificate/test-root', '--timeout=120s')
            root = base64.b64decode(resource('secret', 'test-root', namespace)['data']['tls.crt']).decode()
            apply([{'apiVersion': 'cert-manager.io/v1', 'kind': 'Issuer',
                    'metadata': {'name': 'test-ca', 'namespace': namespace}, 'spec': {'ca': {'secretName': 'test-root'}}}])
            current_source = source_zip()
            for target in (namespace, 'platform'):
                apply([{'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'test-source', 'namespace': target},
                        'binaryData': {'app.zip': current_source}}])
            def patched(documents):
              for doc in documents[1:]:
                if doc['kind'] != 'Deployment' or not any(n in doc['metadata']['name'] for n in ('trino-', 'rustfs')):
                    continue
                pod = doc['spec']['template']['spec']
                pod['volumes'].append({'name': 'test-source', 'configMap': {'name': 'test-source'}})
                for container in [*pod.get('initContainers', []), *pod['containers']]:
                    if container['image'] == IMAGE:
                        # python -m searches cwd before PYTHONPATH. Avoid the
                        # cached dependency image's older /srv/app package.
                        container['workingDir'] = '/tmp'
                        container['env'] = [{'name': 'PYTHONPATH', 'value': '/test-source/app.zip'}]
                        container['volumeMounts'].append({'name': 'test-source', 'mountPath': '/test-source', 'readOnly': True})

            signing = ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'fixture-signing', 'namespace': 'platform'},
                    'stringData': {'private.pem': signing}},
                   {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'fdp-trino-internal', 'namespace': namespace},
                    'stringData': {'internal-secret': uuid.uuid4().hex + uuid.uuid4().hex}},
                   {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'fixture-catalogs', 'namespace': namespace},
                    'stringData': {'tpch.properties': 'connector.name=tpch\n'}},
                   {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'platform-ca', 'namespace': 'platform'},
                    'data': {'ca-bundle.pem': root}},
                   {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'test-fixture', 'namespace': 'platform'},
                    'data': {'policy.py': POLICY, 'probe.py': PROBE, 'monitor.py': MONITOR, 'iceberg.py': ICEBERG}},
                   {'apiVersion': 'v1', 'kind': 'Pod',
                    'metadata': {'name': 'control-plane-fixture', 'namespace': 'platform',
                                 'labels': {'app.kubernetes.io/name': 'fdp-control-plane'}},
                    'spec': {'automountServiceAccountToken': False, 'containers': [{'name': 'control-plane', 'image': IMAGE,
                        'workingDir': '/tmp', 'command': ['python', '/fixture/policy.py'], 'env': [
                            {'name': 'PYTHONPATH', 'value': '/test-source/app.zip'},
                            {'name': 'SSL_CERT_FILE', 'value': '/trust/ca-bundle.pem'},
                            {'name': 'FDP_DATABASE_URL', 'value': 'sqlite://'},
                            {'name': 'FDP_LAYER_NAMESPACE_PREFIX', 'value': config.namespace_prefix},
                            {'name': 'TEST_LAYER', 'value': layers.NON_PROD},
                            # Report a stuck issuance after 20 seconds instead of 30 minutes.
                            {'name': 'FDP_CERTIFICATE_ISSUING_TIMEOUT_SECONDS', 'value': '20'},
                            {'name': 'FDP_TRINO_JWT_PRIVATE_KEY', 'valueFrom': {'secretKeyRef': {'name': 'fixture-signing', 'key': 'private.pem'}}}],
                        'volumeMounts': [{'name': n, 'mountPath': p, 'readOnly': True} for n,p in (
                            ('test-source','/test-source'), ('fixture','/fixture'), ('trust','/trust'))]}],
                        'volumes': [{'name': n, 'configMap': {'name': cm}} for n,cm in (
                            ('test-source','test-source'), ('fixture','test-fixture'), ('trust','platform-ca'))]}},
                   {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'fdp-control-plane', 'namespace': 'platform'},
                    'spec': {'selector': {'app.kubernetes.io/name': 'fdp-control-plane'}, 'ports': [{'port': 8000}]}}])
            # What the controller generates once in the layer namespace (this test applies manifests directly).
            patched(docs)
            # Java trust for Trino (and the RustFS clients): a versioned Secret, replaced with overlap on rotation.
            apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'java-trust-v1', 'namespace': namespace},
                    'stringData': {'ca.pem': root}}])
            apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': namespace}, 'stringData': data}
                   for name, data in (('fdp-postgres-credentials', {'postgres-password': uuid.uuid4().hex,
                                                                           'engine-password': uuid.uuid4().hex}),
                                      ('fdp-rustfs-credentials', {'access-key': 'fdp-' + uuid.uuid4().hex[:8],
                                                                        'secret-key': uuid.uuid4().hex}))])
            apply(docs[1:])
            print('Waiting for issued TLS Secret and Trino rollout', flush=True)
            kube('wait', '-n', namespace, '--for=condition=Ready', 'certificate/fdp-trino', '--timeout=120s')
            kube('rollout', 'status', 'deployment/fdp-trino-coordinator', '-n', namespace, '--timeout=180s', timeout=210)
            kube('rollout', 'status', 'deployment/fdp-trino-worker', '-n', namespace, '--timeout=180s', timeout=210)

            def probe():
                return json.loads(kube('exec', '-n', 'platform', 'control-plane-fixture', '--', 'python', '/fixture/probe.py', diagnostics=True))

            first = wait(lambda: (result if result.get('ready') else None) if (result := probe()) else None,
                         'verified Trino HTTPS and query')
            def trino_pods():
                return json.loads(kube('get', 'pods', '-n', namespace, '-l', 'app.kubernetes.io/component=trino', '-o', 'json'))['items']

            def identities():
                return {p['metadata']['name']: (p['metadata']['uid'],
                    [(c['name'], c['containerID'], c['restartCount']) for c in p['status']['containerStatuses']]) for p in trino_pods()}

            identity = identities()
            assert len(identity) == 2, 'Coordinator and worker must both participate'
            original = resource('secret', 'trino-tls', namespace)['data']

            def renew_leaf(wait_for_revision=True, name='fdp-trino', secret='trino-tls'):
                for attempt in range(5):
                    certificate = resource('certificate', name, namespace)
                    revision = certificate['status']['revision']
                    conditions = [c for c in certificate['status']['conditions'] if c['type'] != 'Issuing']
                    # Same status operation as cmctl renew: request issuance,
                    # never fabricate certificate Ready or copy private keys.
                    conditions.append({'type': 'Issuing', 'status': 'True', 'reason': 'ManuallyTriggered',
                        'message': 'Disposable renewal verification', 'observedGeneration': certificate['metadata']['generation'],
                        'lastTransitionTime': datetime.now(timezone.utc).isoformat().replace('+00:00','Z')})
                    try:
                        kube('patch', 'certificate', name, '-n', namespace, '--subresource=status', '--type=merge',
                            '-p', json.dumps({'metadata': {'resourceVersion': certificate['metadata']['resourceVersion']},
                                             'status': {'conditions': conditions}}))
                        break
                    except RuntimeError:
                        if attempt == 4:
                            raise
                        time.sleep(1)
                if not wait_for_revision:
                    return None
                wait(lambda: resource('certificate',name,namespace)['status'].get('revision',0) > revision,
                     'certificate revision advance')
                return resource('secret', secret, namespace)['data']

            print('Requesting real cert-manager renewal and waiting for projected Secret reload', flush=True)
            renewed = renew_leaf()
            assert renewed['tls.key'] != original['tls.key'], 'Always rotation must replace the private key'
            expected = str(x509.load_pem_x509_certificate(base64.b64decode(renewed['tls.crt'])).serial_number)
            second = wait(lambda: (result if result.get('ready') and result.get('serial') == expected else None)
                          if (result := probe()) else None, 'new certificate served through verified HTTPS', seconds=240)
            assert first['serial'] != second['serial']

            print('Iceberg over HTTPS to RustFS, then a RustFS certificate renewal', flush=True)
            kube('rollout', 'status', 'deployment/fdp-rustfs', '-n', namespace, '--timeout=300s', timeout=330, diagnostics=True)
            kube('rollout', 'status', 'deployment/fdp-postgres', '-n', namespace, '--timeout=300s', timeout=330, diagnostics=True)

            def sql(statement):
                return json.loads(kube('exec', '-n', 'platform', 'control-plane-fixture', '--', 'python', '/fixture/iceberg.py',
                                       statement, diagnostics=True).strip().splitlines()[-1])
            for statement in ('CREATE SCHEMA lake.tls', 'CREATE TABLE lake.tls.orders (id integer)',
                              'INSERT INTO lake.tls.orders VALUES 1, 2, 3'):
                result = wait(lambda: (lambda r: r if 'rows' in r else None)(sql(statement)), statement, seconds=180)
            assert sql('SELECT count(*) FROM lake.tls.orders') == {'rows': [[3]]}, 'Iceberg over HTTPS RustFS failed'

            def rustfs():
                return next(p for p in json.loads(kube('get', 'pods', '-n', namespace, '-l', 'app.kubernetes.io/component=rustfs',
                                                       '-o', 'json'))['items'])

            def rustfs_serial():  # inside the pod, the service name resolves to itself (hostAliases)
                return kube('exec', '-n', namespace, rustfs()['metadata']['name'], '-c', 'buckets', '--', 'python', '-c',
                            "import socket,ssl; from cryptography import x509; h='fdp-rustfs." + namespace + ".svc'; "
                            "c=ssl.create_default_context(cafile='/trust/ca.pem'); "
                            "s=c.wrap_socket(socket.create_connection((h,9000),timeout=5),server_hostname=h); "
                            "print(x509.load_der_x509_certificate(s.getpeercert(binary_form=True)).serial_number)").strip()
            before = rustfs_serial()
            restarts = {c['name']: c['restartCount'] for c in rustfs()['status']['containerStatuses']}
            trino_identity = identities()
            renewed_s3 = renew_leaf(name='fdp-rustfs', secret='rustfs-tls')
            expected = str(x509.load_pem_x509_certificate(base64.b64decode(renewed_s3['tls.crt'])).serial_number)
            wait(lambda: rustfs_serial() == expected, 'RustFS serving its renewed certificate', seconds=240)
            after = {c['name']: c['restartCount'] for c in rustfs()['status']['containerStatuses']}
            assert after['rustfs'] == restarts['rustfs'] + 1 and after['buckets'] == restarts['buckets'], (restarts, after)
            assert before != expected
            assert wait(lambda: (lambda r: r if r.get('rows') == [[3]] else None)(sql('SELECT count(*) FROM lake.tls.orders')),
                        'Iceberg read after the RustFS restart', seconds=180)
            assert trino_identity == identities(), 'A RustFS renewal must not restart Trino'
            print('PASS: Iceberg through Trino over verified HTTPS to RustFS; a renewed RustFS certificate is served after '
                  'the sidecar restarts only the RustFS container; data intact; Trino untouched', flush=True)

            print('Rotating issuer CA with overlapping platform trust', flush=True)
            apply([{'apiVersion': 'cert-manager.io/v1', 'kind': 'Certificate',
                'metadata': {'name': 'test-root-next', 'namespace': namespace},
                'spec': {'isCA': True, 'commonName': 'Disposable Replacement CA', 'secretName': 'test-root-next',
                         'duration': '168h', 'issuerRef': {'name': 'selfsigned'}}}])
            kube('wait', '-n', namespace, '--for=condition=Ready', 'certificate/test-root-next', '--timeout=120s')
            next_root = base64.b64decode(resource('secret', 'test-root-next', namespace)['data']['tls.crt']).decode()

            def trust(bundle):
                apply([{'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'platform-ca', 'namespace': 'platform'},
                        'data': {'ca-bundle.pem': bundle, 'previous-ca.pem': root}}])
                expected_digest = hashlib.sha256(bundle.encode()).hexdigest()
                wait(lambda: kube('exec', '-n', 'platform', 'control-plane-fixture', '--', 'python', '-c',
                    "import hashlib; print(hashlib.sha256(open('/trust/ca-bundle.pem','rb').read()).hexdigest())").strip() == expected_digest,
                    'projected platform CA bundle update', seconds=180)

            trust(root + next_root)
            # Documented procedure: Java trust gets both roots first (a new versioned Secret and a controlled
            # Trino rollout); only then does the issuer switch. Trino's internal HTTPS verifies against it.
            apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'java-trust-v2', 'namespace': namespace},
                    'stringData': {'ca.pem': root + next_root}}])
            rotated = spec.model_copy(deep=True)
            rotated.trino.tls.trust_bundle = rotated.trino.tls.trust_bundle.model_copy(update={'name': 'java-trust-v2'})
            rotated_docs = manifests(layers.NON_PROD, 1, rotated, config, workers=workers)
            patched(rotated_docs)
            apply(rotated_docs[1:])
            for deployment in ('fdp-trino-coordinator', 'fdp-trino-worker', 'fdp-rustfs'):
                kube('rollout', 'status', 'deployment/' + deployment, '-n', namespace, '--timeout=300s', timeout=330)
            wait(lambda: probe().get('ready'), 'Trino ready with overlapping Java trust', seconds=300)
            # The trust rollout restarted Trino on purpose, and a worker re-runs discovery once when the coordinator
            # comes back with a new address. Take the "no restarts from here on" snapshot once that has settled.
            def settled_identities():
                try:
                    first = identities()
                    time.sleep(45)
                    second = identities()
                    return second if first == second and probe().get('ready') else None
                except (RuntimeError, KeyError):  # pods restarting
                    return None
            identity = wait(settled_identities, 'Trino stable after the trust rollout', seconds=360)
            kube('patch', 'issuer', 'test-ca', '-n', namespace, '--type=merge',
                 '-p', json.dumps({'spec': {'ca': {'secretName': 'test-root-next'}}}))
            kube('wait', '-n', namespace, '--for=condition=Ready', 'issuer/test-ca', '--timeout=120s')
            replaced = renew_leaf()
            expected = str(x509.load_pem_x509_certificate(base64.b64decode(replaced['tls.crt'])).serial_number)
            wait(lambda: (result if result.get('ready') and result.get('serial') == expected else None)
                 if (result := probe()) else None, 'replacement CA certificate reload', seconds=240)
            # The real readiness probe must reject the new issuer with old-only trust.
            kube('exec', '-n', 'platform', 'control-plane-fixture', '--', 'python', '-c',
                 "import os; os.environ['SSL_CERT_FILE']='/trust/previous-ca.pem'; "
                 "from app.layer_controller import KubernetesResources; "
                 "assert not KubernetesResources(api=object()).verify_https(" + repr('fdp-trino.'+namespace+'.svc') + ')', diagnostics=True)
            trust(next_root)
            assert probe()['ready'], 'Removing the old root must preserve verified access to the new issuer'
            assert identity == identities(), 'Renewal must not restart Trino nodes or sidecars'
            assert set(kube('get', 'secrets', '-n', 'platform', '-o', 'name').split()) == {'secret/fixture-signing'}
            print('PASS: cert-manager issuance/renewal, key rotation, kubelet projection, encrypted worker query, CA replacement with overlap, old-root rejection, and no Trino restarts', flush=True)

            def monitor():
                status = resource('certificate', 'fdp-trino', namespace).get('status', {})
                return json.loads(kube('exec', '-n', 'platform', 'control-plane-fixture', '--', 'python', '/fixture/monitor.py',
                                       json.dumps(status), 'fdp-trino.' + namespace + '.svc', diagnostics=True)
                                  .strip().splitlines()[-1]), status

            print('Checking renewal scheduling, then breaking and repairing the issuer', flush=True)
            healthy, status = monitor()
            assert healthy['state'] == 'ok', healthy
            policy = spec.trino.tls
            not_after = datetime.fromisoformat(status['notAfter'].replace('Z', '+00:00'))
            renewal = datetime.fromisoformat(status['renewalTime'].replace('Z', '+00:00'))
            assert abs((not_after - renewal).total_seconds() - policy.renew_before_hours * 3600) < 5, (status, policy)
            assert healthy['renewal_time'] == renewal.isoformat(), healthy
            kube('patch', 'issuer', 'test-ca', '-n', namespace, '--type=merge',
                 '-p', json.dumps({'spec': {'ca': {'secretName': 'missing-ca'}}}))
            renew_leaf(wait_for_revision=False)
            broken = wait(lambda: (lambda m: m if m['state'] == 'renewal_failed' else None)(monitor()[0]),
                          'renewal failure reported', seconds=240)
            assert broken['serial'] == healthy['serial'] and 'pending' in broken['message'], broken
            assert probe()['ready'] and probe()['serial'] == healthy['serial'], 'Trino must keep serving its valid certificate'
            kube('patch', 'issuer', 'test-ca', '-n', namespace, '--type=merge',
                 '-p', json.dumps({'spec': {'ca': {'secretName': 'test-root-next'}}}))
            kube('wait', '-n', namespace, '--for=condition=Ready', 'issuer/test-ca', '--timeout=120s')
            recovered = renew_leaf()
            expected = str(x509.load_pem_x509_certificate(base64.b64decode(recovered['tls.crt'])).serial_number)
            wait(lambda: (lambda m: m if m['state'] == 'ok' and m['serial'] == expected else None)(monitor()[0]),
                 'monitor back to ok with the new certificate', seconds=240)
            def settled():  # a query in flight while Trino reloads its certificate can be interrupted
                try:
                    return probe()
                except RuntimeError:
                    return {}
            wait(lambda: (lambda r: r if r.get('ready') and r.get('serial') == expected else None)(settled()),
                 'queries on the renewed certificate', seconds=180)
            assert identity == identities(), 'Recovery must not restart Trino'
            print('PASS: renewal scheduled at notAfter - renewBefore; a broken issuer is reported as renewal_failed while the '
                  'valid certificate keeps serving; after repair a new certificate is served and the state is ok', flush=True)

            print('Replacing disposable coordinator and checking encrypted worker recovery', flush=True)
            coordinator = next(p for p in trino_pods() if p['metadata']['labels']['fdp.floren.app/trino-role'] == 'coordinator')
            worker = next(p for p in trino_pods() if p['metadata']['labels']['fdp.floren.app/trino-role'] == 'worker')
            old_restarts = next(c['restartCount'] for c in worker['status']['containerStatuses'] if c['name'] == 'trino')
            kube('delete', 'pod', coordinator['metadata']['name'], '-n', namespace, '--wait=true')
            kube('rollout', 'status', 'deployment/fdp-trino-coordinator', '-n', namespace, '--timeout=180s', timeout=210)
            replacement = next(p for p in trino_pods() if p['metadata']['labels']['fdp.floren.app/trino-role'] == 'coordinator')
            assert replacement['metadata']['uid'] != coordinator['metadata']['uid']
            if replacement['status']['podIP'] != coordinator['status']['podIP']:
                wait(lambda: next(c['restartCount'] for c in resource('pod', worker['metadata']['name'], namespace)
                                  ['status']['containerStatuses'] if c['name'] == 'trino') > old_restarts,
                     'worker discovery restart after coordinator IP replacement', seconds=180)
            wait(lambda: probe().get('ready'), 'verified worker query after coordinator replacement', seconds=240)
            print('PASS: coordinator replacement and encrypted worker recovery via headless discovery', flush=True)
        except Exception:
            # Only names/reasons and the certificate writer's sanitized logs.
            # Never dump Pod specs, environment, Secret objects or Trino configs.
            try:
                pods = json.loads(kube('get', 'pods', '-A', '-o', 'json'))['items']
                for pod in pods:
                    ns, name = pod['metadata']['namespace'], pod['metadata']['name']
                    if ns.startswith('dc-cert-layer') or ns == 'platform':
                        states = [{'name': c['name'], 'state': next(iter(c.get('state', {})), 'unknown'),
                                   'exit': c.get('state', {}).get('terminated', {}).get('exitCode'),
                                   'reason': c.get('state', {}).get('waiting', {}).get('reason')}
                                  for c in [*pod['status'].get('initContainerStatuses', []), *pod['status'].get('containerStatuses', [])]]
                        print(json.dumps({'namespace': ns, 'pod': name, 'states': states}), flush=True)
                        for container in ('tls-init', 'tls-bundle'):
                            result = subprocess.run(['kubectl','--kubeconfig',kubeconfig,'logs','-n',ns,name,
                                '-c',container,'--tail=12'], env=env, capture_output=True, text=True, timeout=15)
                            if result.returncode == 0:
                                print(result.stdout, flush=True)
            except Exception:
                pass
            raise
        finally:
            if created or subprocess.run(['docker','inspect','k3d-'+cluster+'-server-0'], capture_output=True).returncode == 0:
                subprocess.run(['k3d','cluster','delete',cluster], env=env, capture_output=True)


if __name__ == '__main__':
    main()
