"""Actual Trino 484 HTTPS/JWT/renewal, using disposable Docker resources.

The policy server is an allow-all fixture: this proves transport authentication,
not RBAC. Real layer policy-context isolation has separate control-plane tests.
"""
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[4]
os.environ['FDP_DATABASE_URL'] = 'sqlite://'
os.environ.pop('FDP_DATABASE_HOST', None)
sys.path.insert(0, str(ROOT/'services/control-plane'))
from app import layers, sql_access, tls_bundle
from app.layer_infrastructure import InfrastructureSpec
from app.layer_manifests import ControllerConfig, WorkerPlan, manifests, WORKER_HEALTH
from tests.certificate_helpers import certificate_pair, write_pair


NODE_TLS_PROBE = '''import io.airlift.node.AddressToHostname;
import io.airlift.security.mtls.AutomaticMtls;
import java.net.InetAddress;
import java.util.List;
import javax.net.ssl.*;
class NodeTlsProbe {
 public static void main(String[] args) throws Exception {
  boolean trusted=Boolean.parseBoolean(args[2]);
  SSLContext context=SSLContext.getInstance("TLS");
  context.init(null,new TrustManager[]{AutomaticMtls.createTrustManager(args[0],args[1])},null);
  try (SSLSocket socket=(SSLSocket)context.getSocketFactory().createSocket("127.0.0.1",8443)) {
   socket.setSoTimeout(5000);
   SSLParameters parameters=socket.getSSLParameters();
   parameters.setEndpointIdentificationAlgorithm("HTTPS");
   parameters.setServerNames(List.of(new SNIHostName(AddressToHostname.encodeAddressAsHostname(InetAddress.getByName("127.0.0.1")))));
   socket.setSSLParameters(parameters);
   try { socket.startHandshake(); }
   catch (SSLException | SecurityException rejected) {
    if (trusted) throw new AssertionError("Expected native node trust to succeed",rejected);
    System.out.println("PASS: foreign node trust rejected"); return;
   }
   if (!trusted) throw new AssertionError("Foreign node trust unexpectedly accepted");
   System.out.println("PASS: verified native node TLS "+socket.getSession().getProtocol());
  }
 }
}
'''


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('Disposable TLS fixture command failed: ' + Path(args[0]).name)
    return result.stdout.strip()


def verify_worker_health(image):
    """Exercise the actual pinned image's tools, including a hung DNS resolver."""
    with tempfile.TemporaryDirectory(prefix='fdp-worker-health-') as temporary:
        root = Path(temporary)
        (root/'discovery-address').write_text('10.42.0.5\n')
        (root/'health.sh').write_text(WORKER_HEALTH.replace('/data/trino', '/fixture'))
        getent = root/'getent'
        getent.write_text('''#!/bin/bash
case "$DNS_MODE" in
  slow) sleep 10; printf '10.42.0.6 STREAM\\n' ;;
  moved) printf '10.42.0.6 STREAM\\n' ;;
  same) printf '10.42.0.5 STREAM\\n' ;;
esac
''')
        getent.chmod(0o755)
        for mode, expected in [('same', 0), ('moved', 1), ('slow', 0)]:
            started = time.monotonic()
            result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--user', '1000:1000',
                '-v', str(root)+':/fixture:ro', '-e', 'DNS_MODE='+mode, '-e', 'TRINO_DISCOVERY_HOST=coordinator.test',
                '-e', 'PATH=/fixture:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                '--entrypoint', 'bash', image, '/fixture/health.sh'], capture_output=True, text=True, timeout=8)
            assert result.returncode == expected, 'Worker liveness result incorrect for '+mode+' DNS'
            assert time.monotonic()-started < 7, 'Worker DNS check exceeded its bounded lookup budget'
        print('PASS: pinned-image worker health detects replacement and tolerates bounded DNS hangs', flush=True)


def main():
    java_test = os.environ.get('FDP_TEST_JAVA_TRUST') == '1'
    suffix = uuid.uuid4().hex[:8]
    network = 'dc-tls-' + suffix
    containers = []
    image = os.environ.get('FDP_NETWORK_TEST_SERVER_IMAGE', 'fdp/control-plane:k3d-test')
    config = ControllerConfig('platform', 'tls-test', 'dc-layer', 'runtime', artifact_image=image)
    hostname = 'fdp-trino.dc-layer-%s.svc' % layers.NON_PROD
    spec = InfrastructureSpec(enabled=True, trino={'coordinator': {'cpu': .5, 'memory': '1Gi'},
                                                  'tls': {'mode': 'existing', 'secret_name': 'tls',
                                                    **({'trust_bundle': {'name': 'layer-ca-v1', 'key': 'ca.pem'}} if java_test else {})}})
    docs = manifests(layers.NON_PROD, 1, spec, config, workers=WorkerPlan('test', 0.5, 1, 2))
    verify_worker_health(spec.trino.image)
    signer = ec.generate_private_key(ec.SECP256R1())
    kid = sql_access._key_id(signer.public_key())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(signer.public_key()))
    jwk['kid'] = kid
    with tempfile.TemporaryDirectory(prefix='fdp-trino-tls-') as temporary:
        root = Path(temporary)
        incoming, runtime = root/'incoming', root/'runtime'
        runtime.mkdir()
        key, certificate, ca = certificate_pair(hostname)
        write_pair(incoming, key, certificate)
        assert tls_bundle.publish(incoming, runtime/'keystore.pem', hostname)
        ca_path = root/'ca.pem'
        ca_path.write_bytes(ca[1].public_bytes(serialization.Encoding.PEM))
        (root/'NodeTlsProbe.java').write_text(NODE_TLS_PROBE)
        if java_test:
            # Run the actual tokenless init command in the dependency image with
            # current sources; the truststore must also work for Trino's JVM.
            runtime.chmod(0o777)  # Disposable bind mount substitutes for fsGroup on emptyDir.
            command(['docker', 'run', '--rm', '--network', 'none', '--user', '1000:1000',
                '-e', 'PYTHONDONTWRITEBYTECODE=1', '-v', str(ROOT/'services/control-plane/app')+':/srv/app:ro',
                '-v', str(ca_path)+':/java-ca/ca.pem:ro', '-v', str(runtime)+':/java-trust',
                '--entrypoint', 'python', image, '-m', 'app.java_trust',
                '--input', '/java-ca/ca.pem', '--output', '/java-trust/truststore.p12'])
        helper = root/'policy.py'
        helper.write_text('''from http.server import BaseHTTPRequestHandler,HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(JWKS.encode())
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  action=body.get('input',{}).get('action',{})
  result=list(range(len(action['filterResources']))) if 'filterResources' in action else ([] if self.path.endswith('row-filters') else True)
  self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(json.dumps({'result':result}).encode())
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''.replace('JWKS.encode()', repr(json.dumps({'keys': [jwk]})) + '.encode()'))
        network_created = False
        try:
            command(['docker', 'network', 'create', network]); network_created = True
            if java_test:
                print('Starting isolated PostgreSQL sources with trusted and untrusted CAs', flush=True)
                for source, authority in [('source', ca), ('untrusted', None)]:
                    directory = root/source
                    directory.mkdir()
                    pgkey, pgcert, _ = certificate_pair(source + '.test', ca=authority)
                    write_pair(directory, pgkey, pgcert)
                    name = 'dc-tls-' + source + '-' + suffix
                    command(['docker', 'run', '--rm', '-d', '--name', name, '--network', network,
                        '--network-alias', source + '.test', '--network-alias', source + '-wrong.test',
                        '--tmpfs', '/var/lib/postgresql/data', '-e', 'POSTGRES_PASSWORD=disposable-test-password',
                        '-v', str(directory)+':/certificate:ro', '--entrypoint', 'sh', 'postgres:16-alpine', '-c',
                        'cp /certificate/tls.key /tmp/server.key && cp /certificate/tls.crt /tmp/server.crt '
                        '&& chown postgres:postgres /tmp/server.key /tmp/server.crt && chmod 600 /tmp/server.key '
                        '&& exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key'])
                    containers.append(name)
                    for _ in range(60):
                        check = subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'postgres'], capture_output=True)
                        if check.returncode == 0:
                            break
                        time.sleep(1)
                    else:
                        raise AssertionError('Disposable PostgreSQL did not start')
                    command(['docker', 'exec', name, 'psql', '-U', 'postgres', '-c',
                             'CREATE TABLE trust_probe(value integer); INSERT INTO trust_probe VALUES (42)'])
            helper_name = 'dc-tls-policy-' + suffix
            command(['docker', 'run', '--rm', '-d', '--name', helper_name, '--network', network,
                '--network-alias', 'fdp-control-plane.platform.svc', '-v', str(helper)+':/policy.py:ro',
                '--entrypoint', 'python', image, '/policy.py'])
            containers.append(helper_name)
            for node in ('coordinator', 'worker', 'worker-2'):
                role = 'coordinator' if node == 'coordinator' else 'worker'
                directory = root/node
                directory.mkdir()
                (directory/'catalog').mkdir()
                (directory/'catalog'/'tpch.properties').write_text('connector.name=tpch\ntpch.splits-per-node=2\n')
                if java_test:
                    for catalog, host in [('verified', 'source.test'), ('wrong_host', 'source-wrong.test'), ('untrusted', 'untrusted.test')]:
                        (directory/'catalog'/(catalog+'.properties')).write_text(
                            'connector.name=postgresql\nconnection-url=jdbc:postgresql://' + host +
                            ':5432/postgres?sslmode=verify-full&sslfactory=org.postgresql.ssl.DefaultJavaSSLFactory\n'
                            'connection-user=postgres\nconnection-password=disposable-test-password\n')
                data = next(doc['data'] for doc in docs if doc['kind'] == 'ConfigMap' and doc['metadata']['name'] == 'fdp-trino-' + role)
                for name, content in data.items():
                    (directory/name).write_text(content)
                name = 'dc-tls-' + node + '-' + suffix
                container = next(doc['spec']['template']['spec']['containers'][0] for doc in docs
                    if doc['kind'] == 'Deployment' and doc['metadata']['name'] == 'fdp-trino-' + role)
                args = ['docker', 'run', '-d', '--name', name, '--network', network,
                    '--memory', '1536m', '-e', 'TRINO_INTERNAL_SECRET=disposable-node-secret-' + suffix,
                    '-v', str(directory)+':/etc/trino:ro', '-v', str(runtime)+':/tls-runtime:ro']
                if java_test:
                    args += ['-v', str(runtime)+':/java-trust:ro']
                for entry in container['env']:
                    if 'value' in entry:
                        args += ['-e', entry['name']+'='+entry['value']]
                if role == 'coordinator':
                    args += ['--network-alias', 'fdp-trino',
                        '-v', str(root/'NodeTlsProbe.java')+':/fixture/NodeTlsProbe.java:ro',
                        '--network-alias', 'fdp-trino-discovery.dc-layer-%s.svc' % layers.NON_PROD,
                        '-p', '127.0.0.1::8443', '-p', '127.0.0.1::8080']
                if container.get('command'):
                    args += ['--entrypoint', container['command'][0]]
                command([*args, spec.trino.image, *container.get('command', [])[1:]]); containers.append(name)
            coordinator = 'dc-tls-coordinator-' + suffix
            https = 'https://localhost:' + command(['docker', 'port', coordinator, '8443/tcp']).rsplit(':', 1)[1]
            http = 'http://localhost:' + command(['docker', 'port', coordinator, '8080/tcp']).rsplit(':', 1)[1]
            context = ssl.create_default_context(cafile=str(ca_path))
            print('Waiting for coordinator and encrypted worker discovery', flush=True)
            with httpx.Client(base_url=https, verify=context, timeout=10, follow_redirects=False) as client:
                deadline = time.monotonic()+180
                for _ in range(100):
                    if command(['docker', 'inspect', coordinator, '--format', '{{.State.Running}}']) != 'true':
                        logs = command(['docker', 'logs', coordinator])
                        errors = [line for line in logs.splitlines() if 'was not used' in line or 'must be set' in line]
                        raise AssertionError('Trino coordinator exited; configuration diagnostics: '+str(errors[:8]))
                    try:
                        response = client.get('/v1/info')
                        if response.status_code == 200 and response.json().get('starting') is False:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise AssertionError('Trino HTTPS startup deadline exceeded')
                    time.sleep(1)
                else:
                    raise AssertionError('Trino HTTPS did not start')
                worker = 'dc-tls-worker-' + suffix
                print('Coordinator ready; checking worker discovery bootstrap', flush=True)
                diagnostic = subprocess.run(['docker', 'exec', worker, 'bash', '-c',
                    'getent ahosts "$TRINO_DISCOVERY_HOST"; cat /data/trino/discovery-address'], capture_output=True, text=True)
                print('Worker DNS/bootstrap: '+diagnostic.stdout[:500]+' '+diagnostic.stderr[:200], flush=True)

                def token(audience=None, key=signer):
                    return jwt.encode({'sub': 'tls-test', 'iss': 'fdp-sql',
                        'aud': audience or 'trino-layer-' + layers.NON_PROD, 'exp': int(time.time())+300},
                        key, algorithm='ES256', headers={'kid': kid})

                for invalid in ('forged.token.value', token('trino-layer-' + layers.PROD),
                                token(key=ec.generate_private_key(ec.SECP256R1()))):
                    response = client.post('/v1/statement', content='SELECT 42',
                        headers={'Authorization': 'Bearer ' + invalid, 'X-Trino-User': 'tls-test'})
                    assert response.status_code == 401, 'HTTPS must reject forged, wrong-key and wrong-layer tokens'
                headers = {'Authorization': 'Bearer ' + token(), 'X-Trino-User': 'tls-test',
                           'X-Trino-Extra-Credential': 'dc_context=disposable-fixture',
                           'X-Trino-Session': 'join_distribution_type=PARTITIONED'}
                response = client.post('/v1/statement', content=('SELECT count(*) FROM tpch.tiny.nation n '
                    'JOIN tpch.tiny.region r ON mod(n.nationkey, 5) = r.regionkey'), headers=headers)
                assert response.status_code == 200
                page = response.json()
                rows = list(page.get('data', []))
                nodes = page.get('stats', {}).get('nodes', 0)
                for _ in range(40):
                    assert not page.get('error'), 'Authorized query failed: ' + json.dumps(page.get('error'))
                    if not page.get('nextUri'):
                        break
                    uri = urlsplit(page['nextUri'])
                    page = client.get(uri.path + ('?' + uri.query if uri.query else ''), headers=headers).json()
                    rows.extend(page.get('data', []))
                    nodes = max(nodes, page.get('stats', {}).get('nodes', 0))
                    time.sleep(.1)
                if rows != [[25]] or nodes < 2:
                    logs = subprocess.run(['docker', 'logs', '--tail', '150', worker], capture_output=True, text=True)
                    errors = [line for line in (logs.stdout+logs.stderr).splitlines()
                              if any(word in line for word in ('ERROR', 'Exception', 'Permission denied', 'No such', 'was not used'))]
                    raise AssertionError('Worker query incomplete; bootstrap/registration diagnostics: '+str(errors[-10:]))
                print('Checking native node trust and foreign secret/layer rejection', flush=True)
                for secret, environment, trusted in [
                    ('disposable-node-secret-'+suffix, 'layer_'+layers.NON_PROD, True),
                    ('wrong-node-secret-'+suffix, 'layer_'+layers.NON_PROD, False),
                    ('disposable-node-secret-'+suffix, 'layer_'+layers.PROD, False)]:
                    result = command(['docker', 'exec', coordinator, 'java', '-Xmx128m', '--class-path',
                        '/usr/lib/trino/lib/*', '/fixture/NodeTlsProbe.java', secret, environment, str(trusted).lower()])
                    assert 'PASS:' in result
                if java_test:
                    print('Checking real JDBC verified TLS, wrong-host and untrusted-CA rejection', flush=True)
                    def query(statement):
                        page = client.post('/v1/statement', content=statement, headers=headers).json()
                        result = list(page.get('data', []))
                        for _ in range(100):
                            if not page.get('nextUri') or page.get('error'):
                                return page, result
                            uri = urlsplit(page['nextUri'])
                            page = client.get(uri.path + ('?' + uri.query if uri.query else ''), headers=headers).json()
                            result.extend(page.get('data', []))
                            time.sleep(.1)
                        raise AssertionError('Connector query did not complete')
                    page, result = query('SELECT value FROM verified.public.trust_probe')
                    assert not page.get('error') and result == [[42]], 'Java truststore must authorize the trusted PostgreSQL source'
                    for catalog in ('wrong_host', 'untrusted'):
                        page, _ = query('SELECT value FROM ' + catalog + '.public.trust_probe')
                        assert page.get('error'), 'Java connector must reject ' + catalog
                        detail = json.dumps(page['error']).lower()
                        assert any(word in detail for word in ('ssl', 'certificate', 'pkix')), 'Rejection must be caused by TLS verification'
                try:
                    httpx.post(http+'/v1/statement', headers=headers, content='SELECT 42', timeout=5)
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('TLS-configured Trino must not have a plaintext HTTP listener')
                try:
                    httpx.get(https+'/v1/info', timeout=5)
                except httpx.ConnectError:
                    pass
                else:
                    raise AssertionError('An untrusted CA must be rejected')
                import socket
                try:
                    with socket.create_connection(('127.0.0.1', urlsplit(https).port), timeout=5) as raw:
                        with context.wrap_socket(raw, server_hostname='another-layer.invalid'):
                            pass
                except ssl.SSLCertVerificationError:
                    pass
                else:
                    raise AssertionError('Wrong certificate hostname must be rejected')
                key2, certificate2, _ = certificate_pair(hostname, ca=ca)
                write_pair(incoming, key2, certificate2)
                assert tls_bundle.publish(incoming, runtime/'keystore.pem', hostname)
                expected_serial = x509.load_pem_x509_certificate(certificate2).serial_number
                for _ in range(40):
                    with socket.create_connection(('127.0.0.1', urlsplit(https).port), timeout=5) as raw:
                        with context.wrap_socket(raw, server_hostname='localhost') as secured:
                            serial = x509.load_der_x509_certificate(secured.getpeercert(binary_form=True)).serial_number
                    if serial == expected_serial:
                        break
                    time.sleep(1)
                else:
                    raise AssertionError('Trino did not reload the renewed certificate')
            print('PASS: verified HTTPS, JWT audience/signature rejection, distributed join over native internal TLS, foreign node secret/layer rejection, disabled HTTP listener, wrong CA/SAN rejection, live certificate renewal', flush=True)
            if java_test:
                print('PASS: real PostgreSQL JDBC query using layer Java PKCS12 trust, wrong-host and untrusted-CA rejection', flush=True)
        finally:
            for name in reversed(containers):
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
            if network_created:
                subprocess.run(['docker', 'network', 'rm', network], capture_output=True)


if __name__ == '__main__':
    main()
