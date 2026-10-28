"""End-to-end: real `helm install`/`helm upgrade` of the bundled profile, then a workspace, a job,
an app and standalone Airflow inside one managed layer, all through the TLS gateway.

Disposable k3d cluster, private kubeconfig and temporary files only. Never touches the current
kubectl context, Compose services or their data. Requires the locally built runtime images
(`make images`) plus trinodb/trino:484, postgres:16-alpine, quay.io/keycloak/keycloak:26.6.4 and
traefik:v3.1.6. Refuses to start without enough free Docker disk (FDP_E2E_MIN_FREE_GB).
"""
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import subprocess
import tempfile
import time
import uuid

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[4]
CHART = ROOT / 'deploy/helm/fdp'
NON_PROD = ''  # set from OpenTofu's outputs once the layers exist
HOSTS = ['fdp.test', 'api.fdp.test', 'idp.fdp.test']
PROD = ''
PROVIDER = ROOT / 'terraform/provider-fdp'
PLATFORM_IMAGES = ['trinodb/trino:484', 'postgres:16-alpine', 'quay.io/keycloak/keycloak:26.6.4', 'traefik:v3.1.6',
                   'postgres:16.15-bookworm', 'rustfs/rustfs:1.0.2']  # the last two: layer storage
RUNTIME_IMAGES = ['fdp/compute-python:dev', 'fdp/airflow:dev', 'valkey/valkey:8.0.1']  # last: Celery broker
EDITOR_IMAGE = 'fdp/workspace-vscode:dev'
# The workspace phase adds a 1.9 GB editor image (~4 GB inside the node); opt in when Docker has room.
WORKSPACE = os.environ.get('FDP_E2E_WORKSPACE') == '1'
IMAGES = [*PLATFORM_IMAGES, *RUNTIME_IMAGES, *([EDITOR_IMAGE] if WORKSPACE else [])]

PROGRAMS = {
    'fdp.json': json.dumps({'version': 1, 'python': '3.12', 'warehouse': 'tf-tiny', 'programs': {
        'hello-job': {'entrypoint': 'jobs/hello.py', 'data_sources': ['lake']},
        'hello-job-prod': {'entrypoint': 'jobs/hello.py', 'data_sources': ['lake-prod']},
        'hello-app': {'kind': 'app', 'entrypoint': 'apps/hello.py'},
        'hello-airflow': {'kind': 'airflow', 'entrypoint': 'airflow', 'airflow': '2.10', 'warehouse': 'tf-airflow'}}}),
    'jobs/hello.py': '''import json, os, pathlib, urllib.request
out = pathlib.Path(os.environ["FDP_OUTPUT_DIR"])
(out / "hello.txt").write_text("written inside the layer\\n")
req = urllib.request.Request(os.environ["FDP_API"] + "/v1/sql/query", method="POST",
    headers={"Authorization": "Bearer " + os.environ["FDP_SESSION_TOKEN"], "Content-Type": "application/json"},
    data=json.dumps({"source": os.environ.get("E2E_SOURCE", "lake"), "sql": "SELECT 42 AS answer"}).encode())
rows = json.load(urllib.request.urlopen(req, timeout=60))["rows"]
print("SQL result:", rows)
(out / "sql.json").write_text(json.dumps(rows))
''',
    'apps/hello.py': 'import streamlit as st\nst.title("Hello from a managed layer")\n',
    'airflow/dags/hello.py': '''import pendulum
from airflow import DAG
from fdp.airflow import FDPRunOperator
with DAG(dag_id="hello", schedule=None, start_date=pendulum.datetime(2026, 1, 1, tz="UTC"), catchup=False) as dag:
    FDPRunOperator(task_id="hello_job", program="hello-job")
''',
}


def step(message):
    print(f'[{time.strftime("%H:%M:%S")}] {message}', flush=True)


def command(args, check=True, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if check and result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} {args[1] if len(args) > 1 else ""} failed: {result.stderr[-1500:]}')
    return result.stdout


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wait(check, what, seconds=300, every=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = check()
        except httpx.TransportError:  # e.g. a port-forward that is not listening yet
            value = None
        if value:
            return value
        time.sleep(every)
    raise TimeoutError(what)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def preflight():
    missing = [i for i in IMAGES
               if subprocess.run(['docker', 'image', 'inspect', i], capture_output=True).returncode]
    require(not missing, 'missing local images (run `make images` / docker pull): ' + ', '.join(missing))
    # Images take ~2x their size in the node, and kubelet stops scheduling below ~15% free disk.
    needed = float(os.environ.get('FDP_E2E_MIN_FREE_GB', '20' if WORKSPACE else '16'))
    df = command(['docker', 'run', '--rm', 'alpine:3.20', 'df', '-k', '/']).splitlines()[-1].split()
    free_gb = int(df[3]) / 2**20
    require(free_gb >= needed, f'only {free_gb:.1f} GB free in Docker; need {needed:g} GB (prune or grow its disk)')
    # Docker Desktop's VM disk is a growing file on the host: the host must have room for it too.
    import shutil
    host_gb = shutil.disk_usage(Path.home()).free / 2**30
    require(host_gb >= needed, f'only {host_gb:.1f} GB free on this computer; need {needed:g} GB, or Docker '
                               'turns read-only when its disk file cannot grow')


def tls_material(directory):
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Floren Data Platform e2e root')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=7)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTS[0])]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=7))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in HOSTS]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False).sign(ca_key, hashes.SHA256()))
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM).decode()
    (directory / 'ca.pem').write_text(pem(ca))
    (directory / 'tls.crt').write_text(pem(leaf) + pem(ca))
    (directory / 'tls.key').write_text(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()).decode())
    (directory / 'ca.key').write_bytes(ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))


def issue(directory, name, common_name, issuer=None, ca=False, server_names=()):
    """A certificate signed by issuer (cert, key) or self-signed; written as <name>.pem/<name>.key."""
    now = datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer[0].subject if issuer else subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=7)))
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    if server_names:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in server_names]), critical=False)
    cert = builder.sign(issuer[1] if issuer else key, hashes.SHA256())
    (directory / f'{name}.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / f'{name}.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                              serialization.NoEncryption()))
    return cert, key


def git_repository(directory):
    work = directory / 'work'
    for path, content in PROGRAMS.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(content)
    git = lambda *a, cwd=work: command(['git', *a], cwd=cwd)
    git('init', '-q', '-b', 'main')
    git('add', '.')
    git('-c', 'user.email=e2e@fdp.test', '-c', 'user.name=e2e', 'commit', '-qm', 'E2E programs')
    git('clone', '-q', '--bare', '.', str(directory / 'e2e.git'))
    git('update-server-info', cwd=directory / 'e2e.git')  # dumb-HTTP clone; the control plane only reads


def external_database(cluster, workdir):
    """A company database outside the cluster: a container on the k3d network, outside pod/service ranges.

    Returns the layer network boundaries for the chart and (address, engine password, admin password)."""
    engine, admin = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    (workdir / 'sales.sql').write_text(
        "CREATE ROLE trino_engine LOGIN PASSWORD '%s';\nCREATE SCHEMA shop;\n"
        "CREATE TABLE shop.orders (id integer, amount numeric(10,2));\nINSERT INTO shop.orders VALUES (1, 9.50), (2, 20.00);\n"
        "GRANT USAGE ON SCHEMA shop TO trino_engine;\nGRANT SELECT ON ALL TABLES IN SCHEMA shop TO trino_engine;\n" % engine)
    name = cluster + '-sales-db'
    command(['docker', 'create', '--name', name, '--network', 'k3d-' + cluster, '-e', 'POSTGRES_PASSWORD=' + admin,
             '-e', 'POSTGRES_DB=sales', 'postgres:16-alpine'])
    command(['docker', 'cp', str(workdir / 'sales.sql'), name + ':/docker-entrypoint-initdb.d/sales.sql'])
    command(['docker', 'start', name])
    address = lambda container: command(['docker', 'inspect', container, '-f',
                                         '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}']).strip()
    node = address(f'k3d-{cluster}-server-0')
    network = {'podCidrs': ['10.42.0.0/16'], 'serviceCidrs': ['10.43.0.0/16'], 'nodeCidrs': [node + '/32']}
    return network, (address(name), engine, admin)


def git_server(kubectl, kubeconfig, cluster, workdir):
    """A read-only dumb-HTTP Git server in the platform namespace serving the test repository."""
    kubectl('apply', '-f', '-', input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'git-server', 'namespace': 'fdp', 'labels': {'app': 'git-server'}},
         'spec': {'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001}, 'containers': [{
             'name': 'git', 'image': 'fdp/control-plane:' + cluster, 'imagePullPolicy': 'Never',
             'command': ['python', '-m', 'http.server', '8080', '--directory', '/srv/git'],
             'volumeMounts': [{'name': 'repos', 'mountPath': '/srv/git'}]}], 'volumes': [{'name': 'repos', 'emptyDir': {}}]}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'git-server', 'namespace': 'fdp'},
         'spec': {'selector': {'app': 'git-server'}, 'ports': [{'port': 8080}]}}]}))
    ready = subprocess.run(['kubectl', '--kubeconfig', kubeconfig, '-n', 'fdp', 'wait', '--for=condition=Ready',
                            'pod/git-server', '--timeout=300s'], capture_output=True, text=True)
    require(ready.returncode == 0, 'git server pod not ready:\n' + kubectl('-n', 'fdp', 'describe', 'pod', 'git-server')[-1500:])
    archive = subprocess.run(['tar', 'cf', '-', 'e2e.git'], cwd=workdir, capture_output=True, check=True).stdout
    subprocess.run(['kubectl', '--kubeconfig', kubeconfig, '-n', 'fdp', 'exec', '-i', 'git-server', '--',
                    'tar', 'xf', '-', '-C', '/srv/git'], input=archive, check=True, capture_output=True)


def values(directory, tag, network=None):
    path = directory / 'values.yaml'
    path.write_text(json.dumps({
        'image': {'repository': 'fdp/control-plane', 'tag': tag, 'pullPolicy': 'Never'},
        'publicUrl': 'https://fdp.test', 'apiGateway': {'publicUrl': 'https://api.fdp.test:9443'},
        'tls': {'mode': 'existing'}, 'trust': {'caBundleSecret': 'platform-ca'},
        'metadataDatabase': {'enabled': True, 'image': 'postgres:16-alpine'}, 'bootstrap': {'enabled': True},
        'secretManagement': {'backend': 'kubernetes'}, 'oidc': {'directoryMode': 'scim'},
        # Password grant only so this test can sign in without a browser.
        'keycloak': {'enabled': True, 'publicUrl': 'https://idp.fdp.test', 'passwordGrantEnabled': True,
                     'automationClient': {'enabled': True}},
        'layerController': {'enabled': True, **({'network': network} if network else {})},
        'dataVolume': {'enabled': False}, 'trino': {'enabled': False},
        'certificateMonitoring': {'intervalSeconds': 30},
        'runtime': {'images': {'computePython': RUNTIME_IMAGES[0], 'vscode': EDITOR_IMAGE, 'airflow': RUNTIME_IMAGES[1],
                               'airflowBroker': RUNTIME_IMAGES[2], 'computeDbt': 'fdp/compute-dbt:1.9'}}}))
    return path


class Platform:
    """The installed platform, reached only through the gateway with real hostnames and TLS."""

    def __init__(self, kubeconfig, ca, port, api_port=None):
        self.kubeconfig, self.ca, self.ports = kubeconfig, ca, {443: port, 9443: api_port or port}
        real = socket.getaddrinfo

        def resolve(host, p, *args, **kwargs):
            if isinstance(host, str) and host.endswith('fdp.test'):
                return real('127.0.0.1', self.ports.get(int(p or 443), port), *args, **kwargs)
            return real(host, p, *args, **kwargs)
        socket.getaddrinfo = resolve
        self.http = httpx.Client(verify=ca, timeout=60)
        self.token = None

    def kubectl(self, *args, **kwargs):
        return command(['kubectl', '--kubeconfig', self.kubeconfig, *args], **kwargs)

    def api(self, method, path, **kwargs):
        for attempt in (1, 2):
            response = self.http.request(method, 'https://fdp.test/api' + path,
                                         headers={'Authorization': 'Bearer ' + self.token}, **kwargs)
            if response.status_code != 401 or attempt == 2:
                return response
            self.token = self.login()  # access tokens last 15 minutes; this run takes longer

    def ok(self, method, path, **kwargs):
        response = self.api(method, path, **kwargs)
        require(response.status_code < 300, f'{method} {path}: {response.status_code} {response.text[:300]}')
        return response.json() if response.content else None

    def browser(self, url):
        client = httpx.Client(verify=self.ca, timeout=60, follow_redirects=True)
        return client.get(url), httpx.Client(verify=self.ca, timeout=30).get(url.split('?')[0])

    def sign_in(self):
        """Complete the admin's first-login enrollment (as a person would in the browser), then sign in."""
        pod = self.kubectl('-n', 'fdp', 'get', 'pod', '-l', 'app.kubernetes.io/name=fdp-keycloak',
                           '-o', 'jsonpath={.items[0].metadata.name}')
        kc = lambda *a: self.kubectl('-n', 'fdp', 'exec', pod, '--', 'sh', '-c',
            '/opt/keycloak/bin/kcadm.sh ' + ' '.join(shlex.quote(x) for x in a) + ' --config /tmp/kcadm.config')
        self.kubectl('-n', 'fdp', 'exec', pod, '--', 'sh', '-c', '/opt/keycloak/bin/kcadm.sh config credentials '
            '--config /tmp/kcadm.config --server http://localhost:8080 --realm master '
            '--user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD"')
        user = json.loads(kc('get', 'users', '-r', 'fdp', '-q', 'username=admin', '--fields', 'id'))[0]
        password = 'E2e-' + secrets.token_urlsafe(18)
        kc('set-password', '-r', 'fdp', '--userid', user['id'], '--new-password', password)
        kc('update', 'users/' + user['id'], '-r', 'fdp', '-s', 'requiredActions=[]', '-s', 'firstName=Platform',
           '-s', 'lastName=Admin', '-s', 'email=admin@fdp.test', '-s', 'emailVerified=true')
        self.password = password
        self.token = self.login()
        me = wait(lambda: (lambda r: r.json() if r.status_code == 200 else None)(self.api('GET', '/v1/me')),
                  'SCIM provisioning of the admin', 180)
        require(me.get('is_admin'), 'the bundled admin is not a Floren Data Platform administrator')
        return me

    def login(self):
        response = self.http.post('https://idp.fdp.test/realms/fdp/protocol/openid-connect/token', data={
            'grant_type': 'password', 'client_id': 'fdp-desktop', 'username': 'admin', 'password': self.password})
        require(response.status_code == 200, 'Keycloak sign-in failed: ' + response.text[:200])
        return response.json()['access_token']

    def layer_ready(self, generation):
        def done():
            operation = self.ok('GET', f'/v1/admin/layers/{NON_PROD}/infrastructure')['operation']
            return operation if operation['generation'] == generation and operation['phase'] in ('Ready', 'Failed') else None
        operation = wait(done, 'layer reconciliation', 480, 10)
        require(operation['phase'] == 'Ready', 'layer failed: ' + operation['reason'] + ' ' + operation['message'])

    def deploy(self, repo_id, program):
        pub = self.ok('POST', '/v1/workloads', json={'repo_id': repo_id, 'branch': 'main', 'program': program, 'slug': program})
        final = wait(lambda: (lambda p: p if p['status'] in ('live', 'failed') else None)(
            self.ok('GET', f"/v1/workloads/{pub['id']}")), f'{program} deployment', 600, 5)
        require(final['status'] == 'live', f"{program} did not go live: {final.get('last_error')}")
        return final

    def runs(self):
        listed = self.ok('GET', '/v1/runs', params={'limit': 10})
        return listed if isinstance(listed, list) else listed.get('runs', listed.get('items', []))

    def finished_run(self, run_id):
        run = wait(lambda: (lambda r: r if r['status'] not in ('pending', 'running') else None)(
            self.ok('GET', f'/v1/runs/{run_id}')), 'run completion', 600, 5)
        logs = self.api('GET', f'/v1/runs/{run_id}/logs').text
        require(run['status'] == 'succeeded', f"run {run_id} {run['status']}: {logs[-800:]}")
        require('SQL result: [[42]]' in logs, 'job did not get its SQL result: ' + logs[-800:])
        return run

    def sql_engine_of_last_query(self):
        return self.kubectl('-n', 'fdp', 'exec', 'fdp-metadata-db-0', '--', 'psql', '-U', 'postgres', '-d',
            'fdp', '-Atc', 'SELECT engine_layer FROM sql_queries ORDER BY created_at DESC LIMIT 1').strip()

    def push_programs(self, change, message):
        """Commit a change to fdp.json and deploy it to the in-cluster Git server; returns the new SHA."""
        work = self.workdir / 'work'
        config = json.loads((work / 'fdp.json').read_text())
        change(config['programs'])
        (work / 'fdp.json').write_text(json.dumps(config))
        git = lambda *a, cwd=work: command(['git', *a], cwd=cwd)
        git('-c', 'user.email=e2e@fdp.test', '-c', 'user.name=e2e', 'commit', '-qam', message)
        git('push', '-q', str(self.workdir / 'e2e.git'), 'main')
        git('update-server-info', cwd=self.workdir / 'e2e.git')
        archive = subprocess.run(['tar', 'cf', '-', 'e2e.git'], cwd=self.workdir, capture_output=True, check=True).stdout
        subprocess.run(['kubectl', '--kubeconfig', self.kubeconfig, '-n', 'fdp', 'exec', '-i', 'git-server', '--',
                        'tar', 'xf', '-', '-C', '/srv/git'], input=archive, check=True, capture_output=True)
        return git('rev-parse', 'HEAD').strip()

    def layer_namespace(self):
        return next(n.split('/', 1)[1] for n in self.kubectl('get', 'ns', '-o', 'name').split() if n.endswith(NON_PROD))

    def fingerprint(self):
        digest = lambda s: base64.b64encode(json.dumps(json.loads(self.kubectl('-n', 'fdp', 'get', 'secret', s,
            '-o', 'jsonpath={.data}')), sort_keys=True).encode()).decode()[-24:]
        rows = self.kubectl('-n', 'fdp', 'exec', 'fdp-metadata-db-0', '--', 'psql', '-U', 'postgres', '-d',
            'fdp', '-Atc', 'SELECT (SELECT count(*) FROM workloads), (SELECT count(*) FROM runs), '
            '(SELECT count(*) FROM directory_principals)').strip()
        return {s: digest(s) for s in ('fdp-control-plane', 'fdp-keycloak', 'fdp-metadata-db')}, rows


def https_proxy(gateway_port, routes=None):
    """CONNECT proxy for OpenTofu's process: *.fdp.test goes to the gateway port-forward (TLS stays end to end).

    routes maps other host names (an external IdP) to their own local port-forwards."""
    routes = routes or {}
    import socketserver

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            head = b''
            while b'\r\n\r\n' not in head:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                head += chunk
            target = head.split(b' ', 2)[1].decode()
            host = target.split(':')[0]
            if not head.startswith(b'CONNECT ') or not (host.endswith('fdp.test') or host in routes):
                self.request.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                return
            upstream = socket.create_connection(('127.0.0.1', routes.get(host, gateway_port)))
            self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')

            def pipe(a, b):
                try:
                    while (data := a.recv(65536)):
                        b.sendall(data)
                except OSError:
                    pass
                finally:
                    for s in (a, b):
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
            import threading
            threading.Thread(target=pipe, args=(upstream, self.request), daemon=True).start()
            pipe(self.request, upstream)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads, allow_reuse_address = True, True
    server = Server(('127.0.0.1', 0), Handler)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


TOFU_CONFIG = """
terraform {
  required_providers {
    fdp = { source = "registry.opentofu.org/floren/fdp" }
  }
}

provider "fdp" {
  endpoint  = "https://fdp.test/api"
  token_url = "https://idp.fdp.test/realms/fdp/protocol/openid-connect/token"
  client_id = "fdp-automation"
  ca_file   = "%(ca)s"
}

data "fdp_principal" "admin" { name = "admin" }

locals {
  layer = {
    enabled = true
    # Trino: a coordinator of its own size, and workers on the tf-trino warehouse (each takes one of its slots).
    trino   = { coordinator = { cpu = 0.5, memory = "1Gi" }, workers = { warehouse = fdp_compute_pool.trino.name, min = 1, max = 2 } }
    quota   = { cpu = 8, memory = "20Gi", pods = 30 }
    storage = { workspace_access_mode = "ReadWriteOnce", workspace_size = "1Gi",
                artifact_access_mode = "ReadWriteOnce", artifact_size = "2Gi" }
    # Iceberg on layer storage: catalog in the layer PostgreSQL, data in a layer RustFS bucket.
    # Their credentials are generated by the controller inside each layer namespace.
    postgres = { enabled = true, cpu = 0.25, memory = "256Mi", size = "1Gi" }
    rustfs   = { enabled = true, image = "rustfs/rustfs:1.0.2", cpu = 0.25, memory = "384Mi", size = "1Gi", buckets = ["lake"] }
  }
}

# The platform has no built-in layers: they all come from configuration.
resource "fdp_layer" "prod" {
  name           = "Prod"
  production     = true
  infrastructure = jsonencode(merge(local.layer, { iceberg = { enabled = true, catalog = "lake_prod", bucket = "lake" } }))
}

resource "fdp_layer" "nonprod" {
  name           = "Non-Prod"
  infrastructure = jsonencode(merge(local.layer, { iceberg = { enabled = true, catalog = "lake", bucket = "lake" } }))
}

resource "fdp_default_layer" "this" {
  layer_id = fdp_layer.nonprod.id
}

resource "fdp_default_warehouse" "this" {
  name = fdp_compute_pool.tiny.name
}

resource "fdp_compute_pool" "trino" {
  name      = "tf-trino"
  cpu       = 0.5
  memory_gb = 1
}

resource "fdp_compute_pool" "tiny" {
  name      = "tf-tiny"
  cpu       = 0.5
  memory_gb = 0.5
}

resource "fdp_compute_pool" "airflow" {
  name      = "tf-airflow"
  cpu       = 1
  memory_gb = 2
}

resource "fdp_data_source" "lake" {
  name     = "lake"
  kind     = "iceberg"
  layer_id = fdp_layer.nonprod.id
  config   = jsonencode({ catalog = "lake", warehouse = "s3://lake/", catalog_uri = "http://iceberg-rest:8181" })
}

resource "fdp_data_source" "lake_prod" {
  name     = "lake-prod"
  kind     = "iceberg"
  layer_id = fdp_layer.prod.id
  config   = jsonencode({ catalog = "lake_prod", warehouse = "s3://lake-prod/", catalog_uri = "http://iceberg-rest:8181" })
}

resource "fdp_schema" "nonprod" {
  data_source = fdp_data_source.lake.name
  name        = "reporting"
}

resource "fdp_schema" "prod" {
  data_source = fdp_data_source.lake_prod.name
  name        = "reporting"
}

resource "fdp_role_assignment" "admin_nonprod" {
  resource_id  = fdp_schema.nonprod.id
  principal_id = data.fdp_principal.admin.id
  role         = "Admin"  # creates the Iceberg table
}

resource "fdp_role_assignment" "admin_prod" {
  resource_id  = fdp_schema.prod.id
  principal_id = data.fdp_principal.admin.id
  role         = "Reader"
}

resource "fdp_repository" "e2e" {
  name = "e2e"
  url  = "http://git-server.fdp.svc:8080/e2e.git"
}

resource "fdp_workload" "job" {
  repository_id = fdp_repository.e2e.id
  branch        = "main"
  program       = "hello-job"
  slug          = "hello-job"
  layer_id      = fdp_layer.nonprod.id
  warehouse     = fdp_compute_pool.tiny.name
}

resource "fdp_workload" "job_prod" {
  repository_id = fdp_repository.e2e.id
  branch        = "main"
  program       = "hello-job-prod"
  slug          = "hello-job-prod"
  layer_id      = fdp_layer.prod.id
  warehouse     = fdp_compute_pool.tiny.name
  env           = { E2E_SOURCE = "lake-prod" }
}

resource "fdp_workload" "app" {
  repository_id = fdp_repository.e2e.id
  branch        = "main"
  program       = "hello-app"
  slug          = "hello-app"
  layer_id      = fdp_layer.nonprod.id
  warehouse     = fdp_compute_pool.tiny.name
}

resource "fdp_workload" "airflow" {
  repository_id = fdp_repository.e2e.id
  branch        = "main"
  program       = "hello-airflow"
  slug          = "hello-airflow"
  layer_id      = fdp_layer.nonprod.id
  warehouse     = fdp_compute_pool.airflow.name
}

resource "fdp_role_assignment" "airflow_reads" {
  resource_id  = fdp_schema.nonprod.id
  principal_id = fdp_workload.airflow.service_principal_id
  role         = "Reader"
}

output "ids" {
  value = {
    job = fdp_workload.job.id, job_prod = fdp_workload.job_prod.id,
    app = fdp_workload.app.id, airflow = fdp_workload.airflow.id,
    airflow_identity = fdp_workload.airflow.service_principal_id,
    schema_nonprod = fdp_schema.nonprod.id,
    prod_layer = fdp_layer.prod.id, nonprod_layer = fdp_layer.nonprod.id
  }
}
"""


def tofu_runner(p, workdir, client_secret=None, config=None):
    plugins = workdir / 'plugins'
    command(['go', 'build', '-o', str(plugins / 'terraform-provider-fdp'), '.'], cwd=PROVIDER, timeout=600)
    (workdir / 'tofurc').write_text('provider_installation {\n  dev_overrides { "registry.opentofu.org/floren/fdp" = "%s" }\n'
                                    '  direct {}\n}\n' % plugins)
    tf = workdir / 'tofu'
    tf.mkdir()
    (tf / 'main.tf').write_text((config or TOFU_CONFIG) % {'ca': workdir / 'ca.pem', 'prod': PROD, 'nonprod': NON_PROD})
    env = {**os.environ, 'TF_CLI_CONFIG_FILE': str(workdir / 'tofurc'), 'TF_IN_AUTOMATION': '1',
           'HTTPS_PROXY': f'http://127.0.0.1:{p.proxy_port}'}
    env['FDP_CLIENT_SECRET'] = client_secret or base64.b64decode(p.kubectl(
        '-n', 'fdp', 'get', 'secret', 'fdp-keycloak', '-o', 'jsonpath={.data.automation-client-secret}')).decode()

    def tofu(*args, check=True):
        result = subprocess.run(['tofu', *args, '-no-color'], cwd=tf, env=env, capture_output=True, text=True, timeout=3600)
        if check and result.returncode:
            raise RuntimeError(f'tofu {args[0]} failed:\n{result.stdout[-4000:]}\n{result.stderr[-2000:]}')
        return result
    return tofu


PROBE = '''import json, socket, sys
result = {}
for host, port in json.loads(sys.argv[1]):
    try:
        socket.create_connection((host, port), timeout=3).close()
        result[host + ':' + str(port)] = 'open'
    except OSError:
        result[host + ':' + str(port)] = 'denied'
print(json.dumps(result))
'''


def isolation(p, ids, namespace, execution, prod_run):
    """Layer isolation on the cluster's CNI: network paths, Kubernetes permissions and delegated credentials."""
    prod = namespace.replace(NON_PROD, PROD)
    pod_ip = lambda ns, selector: p.kubectl('-n', ns, 'get', 'pods', '-l', selector, '-o', 'jsonpath={.items[0].status.podIP}')
    svc = lambda name, ns: f'{name}.{ns}.svc'

    def probe(ns, pod, targets, container=None):
        return json.loads(p.kubectl('-n', ns, 'exec', pod, *(['-c', container] if container else []), '--',
                                    'python', '-c', PROBE, json.dumps(targets)).strip().splitlines()[-1])

    stores = lambda ns: [(svc('fdp-trino', ns), 8080), (pod_ip(ns, 'fdp.floren.app/trino-role=coordinator'), 8080),
                         (svc('fdp-postgres', ns), 5432), (svc('fdp-rustfs', ns), 9000)]
    # Positive controls: every target listens and is reachable from its allowed peers.
    control_plane = p.kubectl('-n', 'fdp', 'get', 'pods', '-l', 'app.kubernetes.io/name=fdp-control-plane',
                              '-o', 'jsonpath={.items[0].metadata.name}')
    airflow_web = (pod_ip(namespace, f"fdp.floren.app/workload={ids['airflow']}"), 8501)
    allowed = probe('fdp', control_plane, [*(t for ns in (namespace, prod) for t in stores(ns)[:3]), airflow_web])
    require(set(allowed.values()) == {'open'}, f'control plane cannot reach layer engines/databases: {allowed}')
    for ns in (namespace, prod):
        trino = p.kubectl('-n', ns, 'get', 'pods', '-l', 'fdp.floren.app/trino-role=coordinator', '-o', 'jsonpath={.items[0].metadata.name}')
        reach = p.kubectl('-n', ns, 'exec', trino, '-c', 'trino', '--', 'bash', '-c',
                          f"timeout 3 bash -c '</dev/tcp/{svc('fdp-rustfs', ns)}/9000' && echo open || echo denied").strip()
        require(reach == 'open', f'Trino in {ns} cannot reach its RustFS')

    # A Non-Prod execution: only the control plane (and DNS) is reachable.
    blocked = [*stores(prod), *stores(namespace), (svc('fdp-metadata-db', 'fdp'), 5432),
               (svc('fdp-keycloak', 'fdp'), 8080), ('kubernetes.default.svc', 443), ('1.1.1.1', 443)]
    seen = probe(namespace, execution, [(svc('fdp-control-plane', 'fdp'), 8000), *blocked])
    require(seen.pop(f"{svc('fdp-control-plane', 'fdp')}:8000") == 'open', 'execution cannot reach the control plane')
    require(set(seen.values()) == {'denied'}, f'a Non-Prod execution reached a protected endpoint: {seen}')
    # The other direction, from a Prod pod.
    holder = p.kubectl('-n', prod, 'get', 'pods', '-l', 'app.kubernetes.io/component=artifacts', '-o', 'jsonpath={.items[0].metadata.name}')
    back = probe(prod, holder, [*stores(namespace), airflow_web])
    require(set(back.values()) == {'denied'}, f'a Prod pod reached Non-Prod: {back}')
    step(f'Network isolation on this CNI: {len(seen)} protected endpoints (services and pod IPs, both layers, platform '
         f'database, IdP, Kubernetes API, internet) refused from an execution; {len(back)} Non-Prod endpoints refused from Prod')

    # Namespace permissions: what each identity can and cannot do.
    def can(identity, verb, resource, ns):
        return p.kubectl('auth', 'can-i', verb, resource, '-n', ns, '--as', identity, check=False).strip() == 'yes'
    runtime = 'system:serviceaccount:fdp:fdp-control-plane'
    controller = 'system:serviceaccount:fdp:fdp-layer-controller'
    expectations = [
        (runtime, 'create', 'pods', namespace, True), (runtime, 'create', 'pods/exec', prod, True),
        (runtime, 'create', 'networkpolicies', namespace, False), (runtime, 'delete', 'networkpolicies', prod, False),
        (runtime, 'patch', 'namespaces', namespace, False), (runtime, 'delete', 'namespaces', prod, False),
        (runtime, 'create', 'rolebindings', namespace, False), (runtime, 'delete', 'persistentvolumeclaims', prod, False),
        (runtime, 'get', 'pods', 'kube-system', False),
        (controller, 'get', 'secrets', prod, False), (controller, 'list', 'secrets', namespace, False),
        (controller, 'create', 'pods', namespace, False), (controller, 'create', 'pods/exec', prod, False),
        (controller, 'delete', 'namespaces', prod, False), (controller, 'delete', 'persistentvolumeclaims', namespace, False),
        *[(f'system:serviceaccount:{ns}:fdp-execution', verb, resource, ns, False)
          for ns in (namespace, prod) for verb, resource in (('list', 'pods'), ('get', 'secrets'), ('create', 'pods'))],
    ]
    wrong = [e[:4] for e in expectations if can(*e[:4]) != e[4]]
    require(not wrong, f'unexpected Kubernetes permissions: {wrong}')
    tokens = p.kubectl('-n', namespace, 'get', 'pod', execution, '-o', 'jsonpath={.spec.automountServiceAccountToken}')
    require(tokens == 'false', 'execution pods mount a Kubernetes token')
    step(f'Namespace permissions: {len(expectations)} checks (runtime cannot change policies, namespaces, RBAC or volumes; '
         'the controller cannot read secrets or run pods; executions have no Kubernetes access)')

    # Delegated credentials and gateways.
    job_prod = p.ok('GET', f"/v1/workloads/{ids['job_prod']}")['resource_id']
    grant = p.api('PUT', f'/v1/rbac/resources/{job_prod}/assignments',
                  json={'principal_id': ids['airflow_identity'], 'role': 'Executor'})
    require(grant.status_code == 422, f'a Non-Prod identity was granted a Prod program: {grant.status_code}')
    call = ('import json, os, sys, urllib.request, urllib.error\n'
            'def go(method, path):\n'
            '    req = urllib.request.Request(os.environ["FDP_API"] + path, method=method, data=b"{}" if method == "POST" else None,\n'
            '        headers={"Authorization": "Bearer " + os.environ["FDP_SESSION_TOKEN"], "Content-Type": "application/json"})\n'
            '    try:\n'
            '        return urllib.request.urlopen(req, timeout=60).status\n'
            '    except urllib.error.HTTPError as e:\n'
            '        return e.code\n'
            'print(json.dumps([go("POST", sys.argv[1]), go("GET", sys.argv[2]), go("GET", sys.argv[3])]))\n')
    codes = json.loads(p.kubectl('-n', namespace, 'exec', execution, '--', 'python', '-c', call,
        f"/v1/workloads/{ids['job_prod']}/runs", f"/v1/runs/{prod_run['id']}/outputs",
        f"/v1/runs/{prod_run['id']}/outputs/sql.json").strip().splitlines()[-1])
    require(all(code in (403, 404) for code in codes), f'a Non-Prod execution token used Prod resources: {codes}')
    # A person with Reader in both layers: their Non-Prod job is still refused Prod data.
    p.ok('PATCH', f"/v1/workloads/{ids['job']}", json={'env': {'E2E_SOURCE': 'lake-prod'}})
    try:
        crossing = p.ok('POST', f"/v1/workloads/{ids['job']}/runs", json={'args': {}})
        done = wait(lambda: (lambda r: r if r['status'] not in ('pending', 'running') else None)(
            p.ok('GET', f"/v1/runs/{crossing['id']}")), 'cross-layer job', 600, 5)
        logs = p.api('GET', f"/v1/runs/{crossing['id']}/logs").text
        require(done['status'] == 'failed' and ('403' in logs or '404' in logs) and 'SQL result' not in logs,
                f"a Non-Prod job reached Prod data for a user with grants in both layers: {done['status']} {logs[-300:]}")
    finally:
        p.ok('PATCH', f"/v1/workloads/{ids['job']}", json={'env': {}})
    step('Delegated credentials: Prod grants to a Non-Prod identity refused; a Non-Prod execution token cannot run Prod '
         'programs or read Prod outputs; a person with grants in both layers is refused Prod data from a Non-Prod job')


def scenario(p, cluster, workdir):
    p.sign_in()
    step('Signed in through the gateway; SCIM-provisioned admin recognized')
    wait(lambda: any(d['name'] == 'service-account-fdp-automation' for d in p.ok('GET', '/v1/rbac/directory')),
         'directory sync of the automation identity', 180)

    tofu = tofu_runner(p, workdir)
    step('OpenTofu apply: both layers isolated, pools, sources, schemas, grants, repository, workloads')
    tofu('apply', '-auto-approve', '-input=false')
    plan = tofu('plan', '-detailed-exitcode', '-input=false', check=False)
    require(plan.returncode == 0, 'second plan on the cluster was not empty:\n' + plan.stdout[-3000:])
    ids = json.loads(tofu('output', '-json', 'ids').stdout)
    global PROD, NON_PROD  # created by OpenTofu: the IDs are the platform's, not fixed ones
    PROD, NON_PROD = ids['prod_layer'], ids['nonprod_layer']
    for layer in (PROD, NON_PROD):
        operation = p.ok('GET', f'/v1/admin/layers/{layer}/infrastructure')['operation']
        require(operation['phase'] == 'Ready', f'layer {layer} is {operation["phase"]}')
    step('Applied with the automation identity (client credentials); immediate re-plan is empty')

    def run_job(workload, layer):
        run = p.ok('POST', f'/v1/workloads/{workload}/runs', json={'args': {}})
        p.finished_run(run['id'])
        require(p.sql_engine_of_last_query() == layer, f'job SQL did not run on the {layer} engine')
        return run
    run = run_job(ids['job'], NON_PROD)
    outputs = {o['path'] for o in p.ok('GET', f"/v1/runs/{run['id']}/outputs")}
    require(outputs == {'hello.txt', 'sql.json'}, f'unexpected outputs {outputs}')
    prod_run = run_job(ids['job_prod'], PROD)
    step('The job ran in Non-Prod and in Prod, each on its own layer Trino; outputs read from the layer volume')

    app = ids['app']
    page, anonymous = p.browser(p.ok('POST', f'/v1/workloads/{app}/open')['url'])
    require(page.status_code == 200 and 'streamlit' in page.text.lower(), f'app page {page.status_code}')
    require(anonymous.status_code == 401, f'app without ticket returned {anonymous.status_code}')
    step('App opened through the gateway ticket flow; anonymous access refused')

    workdir_ = p.workdir
    client_ca = issue(workdir_, 'partner-ca', 'Partner CA', ca=True)
    issue(workdir_, 'partner-client', 'partner-system', issuer=client_ca)
    issue(workdir_, 'rogue-client', 'rogue', issuer=issue(workdir_, 'rogue-ca', 'Rogue CA', ca=True))
    p.ok('PUT', '/v1/admin/service-accounts/e2e-partner', json={'layer_id': NON_PROD, 'description': 'mTLS drill'})
    p.ok('PUT', '/v1/secrets/sa/e2e-partner/partner-ca', json={'kind': 'certificate',
         'certificate': (workdir_ / 'partner-ca.pem').read_text()})
    gw_api = p.ok('POST', '/v1/apis', json={'name': 'E2E orders', 'slug': 'e2e-orders', 'direction': 'internal', 'layer_id': NON_PROD,
        'inbound': {'mtls': {'enabled': True, 'ca_secret': 'sa:e2e-partner/partner-ca'}}})
    p.ok('POST', f"/v1/apis/{gw_api['id']}/operations", json={'name': 'run', 'method': 'POST', 'path': '/run',
         'internal': {'workload_id': ids['job']}})
    job_resource = p.ok('GET', f"/v1/workloads/{ids['job']}")['resource_id']
    p.ok('PUT', f'/v1/rbac/resources/{job_resource}/assignments',
         json={'principal_id': gw_api['identity']['principal_id'], 'role': 'Executor'})
    p.ok('PUT', f"/v1/rbac/resources/{ids['schema_nonprod']}/assignments",  # hello-job reads the lake as the API identity
         json={'principal_id': gw_api['identity']['principal_id'], 'role': 'Reader'})
    url = 'https://api.fdp.test:9443/gw/e2e-orders/run'
    import ssl

    def with_cert(name):  # httpx 0.28 presents client certificates only through an SSL context
        context = ssl.create_default_context(cafile=p.ca)
        context.load_cert_chain(str(workdir_ / f'{name}.pem'), str(workdir_ / f'{name}.key'))
        return httpx.Client(verify=context, timeout=60)
    accepted = with_cert('partner-client').post(url)
    require(accepted.status_code == 202, f'client certificate from the trusted CA was refused: {accepted.status_code} {accepted.text[:200]}')
    require(httpx.Client(verify=p.ca, timeout=60).post(url).status_code == 401, 'API accepted a call without a client certificate')
    require(with_cert('rogue-client').post(url).status_code == 401, 'API accepted a certificate from an untrusted CA')
    step('API gateway mTLS: trusted client certificate accepted; none or untrusted refused')

    ca_cert = x509.load_pem_x509_certificate((workdir_ / 'ca.pem').read_bytes())
    ca_key = serialization.load_pem_private_key((workdir_ / 'ca.key').read_bytes(), password=None)
    renewed, _ = issue(workdir_, 'renewed', HOSTS[0], issuer=(ca_cert, ca_key), server_names=HOSTS)
    secret = p.kubectl('-n', 'fdp', 'create', 'secret', 'tls', 'fdp-public-tls', '--cert', str(workdir_ / 'renewed.pem'),
                       '--key', str(workdir_ / 'renewed.key'), '--dry-run=client', '-o', 'yaml')
    p.kubectl('-n', 'fdp', 'apply', '-f', '-', input=secret)
    def served_serial():
        context = ssl.create_default_context(cafile=p.ca)
        with socket.create_connection(('127.0.0.1', p.ports[443]), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=HOSTS[0]) as tls:
                return x509.load_der_x509_certificate(tls.getpeercert(binary_form=True)).serial_number
    wait(lambda: served_serial() == renewed.serial_number, 'gateway serving the renewed certificate', 300, 5)
    page, _ = p.browser(p.ok('POST', f'/v1/workloads/{app}/open')['url'])
    require(page.status_code == 200, 'app unavailable after certificate renewal')
    step('Public gateway certificate renewed in place: new certificate served without a restart')

    # The platform's own monitoring sees the renewed certificate; these test certificates last 7 days
    # and nothing renews them, so they are rightly reported as expiring.
    def gateway_checks():
        checks = {c['key']: c for c in p.ok('GET', '/v1/admin/certificates')}
        public = checks.get('gateway:public')
        return checks if public and public['serial'] == format(renewed.serial_number, 'x') and 'gateway:api' in checks else None
    checks = wait(gateway_checks, 'certificate monitoring of the renewed gateway certificate', 180, 10)
    for key in ('gateway:public', 'gateway:api'):
        require(checks[key]['state'] == 'expiring' and 'nothing renews it' in checks[key]['message'], f'{key}: {checks[key]}')
    control_plane = p.kubectl('-n', 'fdp', 'get', 'pods', '-l', 'app.kubernetes.io/name=fdp-control-plane',
                              '-o', 'jsonpath={.items[0].metadata.name}')
    scrape = ('import os, urllib.request\n'
              'r = urllib.request.Request("http://127.0.0.1:8000/internal/metrics/certificates",'
              ' headers={"Authorization": "Bearer " + os.environ["FDP_METRICS_TOKEN"]})\n'
              'print(urllib.request.urlopen(r, timeout=10).read().decode())\n')
    metrics = p.kubectl('-n', 'fdp', 'exec', control_plane, '--', 'python', '-c', scrape)
    require('fdp_certificate_problem{certificate="gateway:public"' in metrics and 'state="expiring"} 1' in metrics,
            'certificate metrics missing: ' + metrics[:400])
    step('Certificate monitoring: both gateway certificates checked through the gateway, the renewed one recognized, '
         'short-lived certificates reported as expiring in the admin API and Prometheus metrics')

    namespace = p.layer_namespace()
    pod = p.kubectl('-n', namespace, 'get', 'pods', '-l', f"fdp.floren.app/workload={ids['airflow']}",
                    '-o', 'jsonpath={.items[0].metadata.name}')
    # Same settings as the running scheduler (its DAG folder is the fetched source). New DAGs start
    # paused in Airflow, as an operator would see in its UI: unpause before triggering.
    p.kubectl('-n', namespace, 'exec', pod, '--', 'bash', '-c',
              'export AIRFLOW_HOME=/fdp/airflow AIRFLOW__CORE__DAGS_FOLDER=/app/airflow/dags; '
              'for i in $(seq 1 60); do airflow dags list 2>/dev/null | grep -q "^hello " && break; sleep 5; done; '
              'airflow dags unpause hello >/dev/null && airflow dags trigger hello >/dev/null')
    history = wait(lambda: (lambda h: h if h and h[0]['state'] in ('success', 'failed') else None)(
        p.ok('GET', f"/v1/workloads/{ids['airflow']}/orchestrator-runs").get('runs')), 'DAG run', 900, 10)
    require(history[0]['state'] == 'success', 'DAG run failed')
    started = [r for r in p.runs() if r.get('owner') == 'service:' + ids['airflow_identity']]
    require(started and started[0]['status'] == 'succeeded', 'the DAG did not start hello-job successfully')
    step('Standalone Airflow in Non-Prod started hello-job as its own identity; history read in-layer')
    probe = ('import json, os, urllib.request, urllib.error\n'
             'def ask(source):\n'
             '    req = urllib.request.Request(os.environ["FDP_API"] + "/v1/sql/query", method="POST", headers={\n'
             '        "Authorization": "Bearer " + os.environ["FDP_SESSION_TOKEN"], "Content-Type": "application/json"},\n'
             '        data=json.dumps({"source": source, "sql": "SELECT 42"}).encode())\n'
             '    try:\n'
             '        return json.load(urllib.request.urlopen(req, timeout=60)).get("rows")\n'
             '    except urllib.error.HTTPError as e:\n'
             '        return e.code\n'
             'print(json.dumps({"own": ask("lake"), "other": ask("lake-prod")}))\n')
    answers = json.loads(p.kubectl('-n', namespace, 'exec', pod, '--', 'python', '-c', probe).strip().splitlines()[-1])
    require(answers['own'] == [[42]], f"a Non-Prod execution could not query its own layer: {answers['own']}")
    require(answers['other'] in (403, 404), f"a Non-Prod execution reached Prod data: {answers['other']}")
    step('A Non-Prod execution queried its own layer and was refused Prod data')
    isolation(p, ids, namespace, pod, prod_run)

    # Iceberg on the layer's own storage, through the platform's SQL path.
    def sql(source, statement):
        return p.ok('POST', '/v1/sql/query', json={'source': source, 'sql': statement})
    sql('lake', 'CREATE SCHEMA IF NOT EXISTS lake.reporting')
    sql('lake', 'CREATE TABLE lake.reporting.e2e_orders (id integer, region varchar)')
    sql('lake', "INSERT INTO lake.reporting.e2e_orders VALUES (1, 'north'), (2, 'south')")
    iceberg_rows = lambda: sql('lake', 'SELECT id, region FROM lake.reporting.e2e_orders ORDER BY id')['rows']
    require(iceberg_rows() == [[1, 'north'], [2, 'south']], 'Iceberg table did not read back')
    require(p.sql_engine_of_last_query() == NON_PROD, 'Iceberg SQL did not run on the Non-Prod engine')
    catalog = p.kubectl('-n', namespace, 'exec', 'deploy/fdp-postgres', '-c', 'postgres', '--', 'psql', '-U', 'postgres',
                        '-d', 'iceberg', '-Atc', 'SELECT table_namespace, table_name FROM iceberg_tables').strip()
    require(catalog == 'reporting|e2e_orders', f'Iceberg metadata not in the layer database: {catalog!r}')
    objects = p.kubectl('-n', namespace, 'exec', 'deploy/fdp-rustfs', '-c', 'buckets', '--', 'python', '-c',
        'import boto3, pathlib; k = lambda n: pathlib.Path("/rustfs-auth", n).read_text().strip(); '
        's3 = boto3.client("s3", endpoint_url="http://127.0.0.1:9000", region_name="us-east-1", '
        'aws_access_key_id=k("access-key"), aws_secret_access_key=k("secret-key")); '
        'print(sum(1 for o in s3.list_objects_v2(Bucket="lake").get("Contents", []) if o["Key"].endswith(".parquet")))').strip()
    require(int(objects) >= 1, 'no Iceberg data files in the layer bucket')
    refused = p.api('POST', '/v1/sql/query', json={'source': 'lake', 'sql': 'SELECT count(*) FROM lake_prod.reporting.e2e_orders'})
    require(refused.status_code >= 400, 'the Non-Prod engine reached the Prod Iceberg catalog')
    generated = p.kubectl('-n', namespace, 'get', 'secret', 'fdp-postgres-credentials', 'fdp-rustfs-credentials',
                          '-o', 'name').split()
    require(len(generated) == 2, 'layer storage credentials were not generated in the layer namespace')
    require('fdp-rustfs-credentials' not in p.kubectl('-n', 'fdp', 'get', 'secret', '-o', 'name'),
            'layer storage credentials leaked into the platform namespace')
    step('Iceberg on layer storage: table written and read through the layer Trino; metadata in the layer '
         'PostgreSQL, data in its RustFS bucket; generated credentials stay in the layer; Prod catalog refused')

    # An external database registered as a Non-Prod source: its catalog is generated, not hand-written.
    address, engine_password, admin_password = p.external_db
    p.ok('PUT', '/v1/admin/datasources/ext-sales', json={
        'name': 'ext-sales', 'kind': 'postgres', 'egress': [address + '/32'],
        'config': {'host': address, 'port': 5432, 'database': 'sales', 'catalog': 'ext_sales', 'layer_id': NON_PROD},
        'secret': {'username': 'trino_engine', 'password': engine_password},
        'provisioner_secret': {'username': 'postgres', 'password': admin_password}})
    shop = p.ok('POST', '/v1/rbac/sources/ext-sales/schemas', json={'name': 'shop'})
    p.ok('POST', f"/v1/rbac/resources/{shop['id']}/provision")  # access setup, as fdp_schema does
    external = lambda: p.api('POST', '/v1/sql/query', json={'source': 'ext-sales', 'sql': 'SELECT count(*) FROM ext_sales.shop.orders'})
    rows = wait(lambda: (lambda r: r.json().get('rows') if r.status_code == 200 else None)(external()),
                'generated external source catalog live in the layer Trino', 600, 10)
    require(rows == [[2]] and p.sql_engine_of_last_query() == NON_PROD, f'external source query returned {rows}')
    generated = json.loads(p.kubectl('-n', namespace, 'get', 'secret', 'fdp-source-catalogs', '-o', 'jsonpath={.data}'))
    catalog = base64.b64decode(generated['ext_sales.properties']).decode()
    require(set(generated) == {'ext_sales.properties'} and 'connection-user=trino_engine' in catalog
            and admin_password not in catalog, 'the generated catalog is wrong or carries the access-setup login')
    prod_namespace = namespace.replace(NON_PROD, PROD)
    require(p.kubectl('-n', prod_namespace, 'get', 'secret', 'fdp-source-catalogs', check=False).strip() == '',
            'a Non-Prod source catalog reached Prod')
    reach = json.loads(p.kubectl('-n', namespace, 'exec', pod, '--', 'python', '-c', PROBE,
                                 json.dumps([(address, 5432)])).strip().splitlines()[-1])
    require(set(reach.values()) == {'denied'}, 'an execution reached the external database directly; only Trino is granted')
    step('External source: catalog generated from the engine login into Non-Prod only; queried through the layer '
         'Trino over its egress grant; the access-setup login never reaches the engine; executions cannot reach the database')

    before = p.fingerprint()
    command(['docker', 'tag', 'fdp/control-plane:' + cluster, 'fdp/control-plane:' + cluster + '-upgrade'])
    command(['k3d', 'image', 'import', '-c', cluster, 'fdp/control-plane:' + cluster + '-upgrade'], timeout=600)
    command(['helm', 'upgrade', 'fdp', str(CHART), '-n', 'fdp', '-f', str(workdir / 'values.yaml'),
             '--set', f'image.tag={cluster}-upgrade', '--kubeconfig', p.kubeconfig, '--wait', '--timeout', '10m'], timeout=900)
    images = p.kubectl('-n', 'fdp', 'get', 'deploy', 'fdp-control-plane', '-o',
                       'jsonpath={.spec.template.spec.containers[0].image}')
    require(images.endswith('-upgrade'), 'control plane was not upgraded')
    require(p.fingerprint() == before, 'helm upgrade changed generated secrets or metadata')
    require(p.ok('GET', f"/v1/runs/{run['id']}/outputs/sql.json")['content_base64'] == base64.b64encode(b'[[42]]').decode(),
            'job output lost across the upgrade')
    require(iceberg_rows() == [[1, 'north'], [2, 'south']], 'Iceberg data lost across the upgrade')
    page, _ = p.browser(p.ok('POST', f'/v1/workloads/{app}/open')['url'])
    require(page.status_code == 200, 'app unavailable after the upgrade')
    plan = tofu('plan', '-detailed-exitcode', '-input=false', check=False)
    require(plan.returncode == 0, 'configuration drifted across the upgrade:\n' + plan.stdout[-3000:])
    step('helm upgrade rolled the platform; secrets, metadata, outputs and the app survived; OpenTofu sees no drift')

    # Rollback drill: the documented procedure is `helm rollback` to the previous revision.
    command(['helm', 'rollback', 'fdp', '1', '-n', 'fdp', '--kubeconfig', p.kubeconfig, '--wait', '--timeout', '10m'], timeout=900)
    images = p.kubectl('-n', 'fdp', 'get', 'deploy', 'fdp-control-plane', '-o',
                       'jsonpath={.spec.template.spec.containers[0].image}')
    require(images.endswith(cluster), f'rollback did not restore the previous image: {images}')
    require(p.fingerprint() == before, 'helm rollback changed generated secrets or metadata')
    page, _ = p.browser(p.ok('POST', f'/v1/workloads/{app}/open')['url'])
    require(page.status_code == 200, 'app unavailable after the rollback')
    require(iceberg_rows() == [[1, 'north'], [2, 'south']], 'Iceberg data lost across the rollback')
    plan = tofu('plan', '-detailed-exitcode', '-input=false', check=False)
    require(plan.returncode == 0, 'configuration drifted across the rollback:\n' + plan.stdout[-3000:])
    step('helm rollback restored the previous release; secrets, metadata and the app intact; no drift')

    # Backup drill: dump the metadata database and restore it into a scratch database on the same server.
    dump = p.kubectl('-n', 'fdp', 'exec', 'fdp-metadata-db-0', '--', 'pg_dump', '-U', 'postgres', '-d', 'fdp',
                     '--no-owner', '--no-privileges')
    require('CREATE TABLE' in dump and len(dump) > 10000, 'metadata dump looks empty')
    p.kubectl('-n', 'fdp', 'exec', 'fdp-metadata-db-0', '--', 'psql', '-U', 'postgres', '-c', 'CREATE DATABASE restore_drill')
    subprocess.run(['kubectl', '--kubeconfig', p.kubeconfig, '-n', 'fdp', 'exec', '-i', 'fdp-metadata-db-0', '--',
                    'psql', '-q', '-U', 'postgres', '-d', 'restore_drill', '-v', 'ON_ERROR_STOP=1'],
                   input=dump, text=True, capture_output=True, check=True)
    counts = ('SELECT (SELECT count(*) FROM workloads), (SELECT count(*) FROM runs), (SELECT count(*) FROM rbac_resources), '
              '(SELECT count(*) FROM role_assignments), (SELECT count(*) FROM layer_operations), (SELECT count(*) FROM directory_principals)')
    live = p.kubectl('-n', 'fdp', 'exec', 'fdp-metadata-db-0', '--', 'psql', '-At', '-U', 'postgres', '-d', 'fdp', '-c', counts).strip()
    restored = p.kubectl('-n', 'fdp', 'exec', 'fdp-metadata-db-0', '--', 'psql', '-At', '-U', 'postgres', '-d', 'restore_drill', '-c', counts).strip()
    require(live == restored, f'restored metadata differs: live {live}, restored {restored}')
    p.kubectl('-n', 'fdp', 'exec', 'fdp-metadata-db-0', '--', 'psql', '-U', 'postgres', '-c', 'DROP DATABASE restore_drill')
    step(f'Metadata backup restored into a scratch database with identical row counts ({live})')

    # Rotation drill: new session and layer-Trino keys, the former ones kept for an overlap.
    current = json.loads(p.kubectl('-n', 'fdp', 'get', 'secret', 'fdp-control-plane', '-o', 'jsonpath={.data}'))
    old_session = base64.b64decode(current['FDP_SESSION_SIGNING_KEY']).decode()
    old_trino = base64.b64decode(current['FDP_TRINO_JWT_PRIVATE_KEY']).decode()
    new_trino = ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
    encode = lambda value: base64.b64encode(value.encode()).decode()
    patch = {'data': {'FDP_SESSION_SIGNING_KEY': encode(secrets.token_urlsafe(48)),
                      'FDP_SESSION_SIGNING_PREVIOUS_KEYS': encode(old_session),
                      'FDP_TRINO_JWT_PRIVATE_KEY': encode(new_trino), 'FDP_TRINO_JWT_PREVIOUS_KEYS': encode(old_trino)}}
    p.kubectl('-n', 'fdp', 'patch', 'secret', 'fdp-control-plane', '--type', 'merge', '-p', json.dumps(patch))
    p.kubectl('-n', 'fdp', 'rollout', 'restart', 'deployment/fdp-control-plane')
    p.kubectl('-n', 'fdp', 'rollout', 'status', 'deployment/fdp-control-plane', '--timeout=300s')
    wait(lambda: p.api('GET', '/v1/me').status_code == 200, 'control plane after key rotation', 120, 3)
    run_job(ids['job'], NON_PROD)  # query tokens now carry the new key ID; the layer Trino must refresh its JWKS
    p.kubectl('-n', namespace, 'exec', pod, '--', 'bash', '-c',
              'export AIRFLOW_HOME=/fdp/airflow AIRFLOW__CORE__DAGS_FOLDER=/app/airflow/dags; airflow dags trigger hello >/dev/null')
    previous_runs = len(history)
    history = wait(lambda: (lambda h: h if h and len(h) > previous_runs and h[0]['state'] in ('success', 'failed') else None)(
        p.ok('GET', f"/v1/workloads/{ids['airflow']}/orchestrator-runs").get('runs')), 'DAG run after rotation', 900, 10)
    require(history[0]['state'] == 'success', 'Airflow (token signed with the former key) failed after rotation')
    page, _ = p.browser(p.ok('POST', f'/v1/workloads/{app}/open')['url'])
    require(page.status_code == 200, 'app unavailable after key rotation')
    step('Keys rotated with overlap: new SQL tokens accepted by layer Trino, existing execution tokens still valid')

    # Retirement drill on a spare layer: scale to zero, keep storage, delete only after the operator does.
    p.api('DELETE', f'/v1/workloads/{app}')  # one small node: make room for the spare layer's Trino
    spare = p.ok('POST', '/v1/admin/layers', json={'name': 'Spare'})
    spec = {'enabled': True, 'trino': {'coordinator': {'cpu': 0.5, 'memory': '1Gi'}, 'workers': {'warehouse': 'tf-trino'}},
            'quota': {'cpu': 4, 'memory': '8Gi', 'pods': 20},
            'storage': {'workspace_access_mode': 'ReadWriteOnce', 'artifact_access_mode': 'ReadWriteOnce', 'artifact_size': '1Gi'}}
    p.ok('PUT', f"/v1/admin/layers/{spare['id']}/infrastructure", json={'expected_generation': 0, 'spec': spec})
    def phase(layer, generation):
        operation = p.ok('GET', f'/v1/admin/layers/{layer}/infrastructure')['operation']
        return operation if operation['generation'] == generation and operation['phase'] in ('Ready', 'Retired', 'Failed') else None
    require(wait(lambda: phase(spare['id'], 1), 'spare layer ready', 480, 10)['phase'] == 'Ready', 'spare layer did not become Ready')
    p.ok('POST', f"/v1/admin/layers/{spare['id']}/infrastructure/retire", json={'expected_generation': 1})
    retired = wait(lambda: phase(spare['id'], 2), 'spare layer retired', 300, 5)
    require((retired['phase'], retired['reason']) == ('Retired', 'StorageRetained'), f'unexpected retirement state {retired}')
    spare_ns = retired['namespace']
    replicas = p.kubectl('-n', spare_ns, 'get', 'deploy', '-o', 'jsonpath={.items[*].spec.replicas}').split()
    require(replicas and set(replicas) == {'0'}, f'retired layer still runs compute: {replicas}')
    require('fdp-artifacts' in p.kubectl('-n', spare_ns, 'get', 'pvc', '-o', 'name'), 'retired layer lost its volume')
    refused = p.api('DELETE', f"/v1/admin/layers/{spare['id']}")
    require(refused.status_code == 409 and spare_ns in refused.text, 'a layer with retained storage was deletable')
    p.kubectl('delete', 'namespace', spare_ns, '--wait=true', '--timeout=180s')  # the operator removes the data
    wait(lambda: (lambda o: o if o['reason'] == 'StorageDeleted' else None)(
        p.ok('GET', f"/v1/admin/layers/{spare['id']}/infrastructure")['operation']), 'controller notices deletion', 600, 10)
    require(spare_ns not in p.kubectl('get', 'ns', '-o', 'name'), 'the controller recreated a deleted namespace')
    require(p.api('DELETE', f"/v1/admin/layers/{spare['id']}").status_code == 204, 'layer not deletable after its storage was removed')
    step(f"Spare layer retired to zero with storage kept, then deleted after the operator removed {spare_ns}")

    # Distributed Airflow in the layer: switch the deployment to Celery mode with a new commit.
    pool = next(x for x in p.ok('GET', '/v1/admin/pools') if x['name'] == 'tf-airflow')
    p.ok('PUT', '/v1/admin/pools/tf-airflow', json={**{k: pool[k] for k in ('name', 'cpu', 'gpu', 'max_concurrent', 'auto_suspend_seconds', 'allowed_groups') if k in pool},
                                                    'memory_gb': 1.5})  # web and scheduler fit beside everything else
    before_runs = len([r for r in p.runs() if r.get('owner') == 'service:' + ids['airflow_identity']])
    sha = p.push_programs(lambda programs: programs['hello-airflow'].update(
        {'airflow_settings': {'mode': 'distributed', 'workers': 1, 'worker_warehouse': 'tf-tiny'}}), 'Distributed Airflow')
    p.ok('POST', f"/v1/workloads/{ids['airflow']}/sync")
    deployed = wait(lambda: (lambda x: x if x.get('deployed_sha') == sha or x.get('failed_sha') == sha else None)(
        p.ok('GET', f"/v1/workloads/{ids['airflow']}")), 'distributed Airflow deployment', 900, 10)
    require(deployed.get('deployed_sha') == sha, f"distributed Airflow did not deploy: {deployed.get('sync_error')}")
    pods = json.loads(p.kubectl('-n', namespace, 'get', 'pods', '-l', f"fdp.floren.app/workload={ids['airflow']}", '-o', 'json'))['items']
    roles = sorted(x['metadata']['labels'].get('fdp.floren.app/airflow-role') or x['metadata']['labels'].get('fdp.floren.app/role')
                   for x in pods)
    require(roles == ['airflow-metadata', 'broker', 'scheduler', 'web', 'worker'], f'unexpected distributed components {roles}')
    web = next(x['metadata']['name'] for x in pods if x['metadata']['labels'].get('fdp.floren.app/airflow-role') == 'web')
    p.kubectl('-n', namespace, 'exec', web, '--', 'bash', '-c',
              'export AIRFLOW__CORE__DAGS_FOLDER=/app/airflow/dags; '
              'for i in $(seq 1 60); do airflow dags unpause hello >/dev/null 2>&1 && break; sleep 5; done; '
              'airflow dags trigger hello >/dev/null')
    history = wait(lambda: (lambda h: h if h and h[0]['state'] in ('success', 'failed') else None)(
        p.ok('GET', f"/v1/workloads/{ids['airflow']}/orchestrator-runs").get('runs')), 'distributed DAG run', 900, 10)
    require(history[0]['state'] == 'success', 'distributed DAG run failed')
    started = [r for r in p.runs() if r.get('owner') == 'service:' + ids['airflow_identity']]
    require(len(started) > before_runs and started[0]['status'] == 'succeeded', 'the Celery worker did not start hello-job')
    broker = next(x['metadata']['name'] for x in pods if x['metadata']['labels'].get('fdp.floren.app/role') == 'broker')
    require('NOAUTH' in p.kubectl('-n', namespace, 'exec', broker, '--', 'valkey-cli', 'PING'), 'the layer broker accepted no password')
    holder = p.kubectl('-n', namespace, 'get', 'pods', '-l', 'app.kubernetes.io/component=artifacts', '-o', 'jsonpath={.items[0].metadata.name}')
    reach = p.kubectl('-n', namespace, 'exec', holder, '--', 'python', '-c',
        'import socket, sys\ntry: socket.create_connection((sys.argv[1], 6379), timeout=4); print("OPEN")\nexcept OSError: print("DENIED")',
        f"pub-{ids['airflow']}-broker").strip()
    require(reach == 'DENIED', 'a non-Airflow pod in the layer reached the broker')
    step('Distributed Airflow in Non-Prod: broker, metadata database, scheduler, web and Celery worker in the layer; '
         'the DAG ran hello-job; the broker needs its password and other layer pods cannot reach it')

    if not WORKSPACE:
        step('Workspace phase skipped (set FDP_E2E_WORKSPACE=1 with ~20 GB free Docker disk)')
        return
    ws = p.ok('POST', '/v1/workspaces', json={'name': 'E2E editor', 'editor': 'vscode', 'layer_id': NON_PROD})
    p.ok('POST', f"/v1/workspaces/{ws['id']}/start")
    state = wait(lambda: (lambda w: w if w['status'] in ('running', 'failed') else None)(
        next(w for w in p.ok('GET', '/v1/workspaces') if w['id'] == ws['id'])), 'workspace start', 600, 5)
    require(state['status'] == 'running', 'workspace failed: ' + str(state.get('startup_error')))
    page, anonymous = p.browser(p.ok('POST', f"/v1/workspaces/{ws['id']}/open")['url'])
    require(page.status_code == 200 and 'code-server' in page.text, f'editor page {page.status_code}')
    require(anonymous.status_code == 401, f'editor without ticket returned {anonymous.status_code}')
    query = lambda source: ('curl -sS -X POST "$FDP_API/v1/sql/query" -H "Authorization: Bearer $FDP_SESSION_TOKEN" '
                            '-H "Content-Type: application/json" -d \'{"source":"%s","sql":"SELECT 42"}\'; echo' % source)
    inside = p.kubectl('-n', namespace, 'exec', 'ws-' + ws['id'], '--', 'sh', '-c',
        query('lake') + '; echo "---"; ' + query('lake-prod') + '; echo "trust=$SSL_CERT_FILE"; '
        'curl -sS -m 5 -o /dev/null -w "internet=%{http_code}" https://pypi.org || true')
    own, other = inside.split('---', 1)
    require('[[42]]' in own, 'workspace SQL on its own layer failed: ' + own[-300:])
    require('[[42]]' not in other and ('detail' in other), 'a Non-Prod workspace reached Prod data: ' + other[-300:])
    require('trust=/etc/fdp/trust/ca-bundle.pem' in inside and 'internet=000' in inside,
            'workspace trust/egress check failed: ' + inside[-300:])
    step('Workspace in Non-Prod: own-layer SQL works, Prod data refused, layer trust present, no internet')


def main():
    preflight()
    cluster = 'dc-e2e-' + uuid.uuid4().hex[:6]
    created, forward = False, None
    with tempfile.TemporaryDirectory(prefix='fdp-e2e-') as temporary:
        workdir = Path(temporary)
        kubeconfig = str(workdir / 'kubeconfig')
        try:
            step('Building the control plane from the current sources')
            command(['docker', 'build', '-q', '-t', 'fdp/control-plane:' + cluster, str(ROOT / 'services/control-plane')], timeout=900)
            tls_material(workdir)
            git_repository(workdir)
            step(f'Creating disposable cluster {cluster}')
            command(['k3d', 'cluster', 'create', cluster, '--agents', '0', '--no-lb', '--kubeconfig-update-default=false',
                     '--kubeconfig-switch-context=false', '--k3s-arg', '--disable=traefik@server:0',
                     '--k3s-arg', '--disable=metrics-server@server:0', '--wait'], timeout=300)
            created = True
            Path(kubeconfig).write_text(command(['k3d', 'kubeconfig', 'get', cluster]))
            step('Importing images')
            # One image at a time: a single combined archive of every image nearly fills Docker's disk.
            for image in ['fdp/control-plane:' + cluster, *IMAGES]:
                started = time.monotonic()
                command(['k3d', 'image', 'import', '-c', cluster, image], timeout=1200)
                step(f'  imported {image} in {time.monotonic() - started:.0f}s')
            kubectl = lambda *a, **kw: command(['kubectl', '--kubeconfig', kubeconfig, *a], **kw)
            kubectl('create', 'namespace', 'fdp')
            kubectl('-n', 'fdp', 'create', 'secret', 'tls', 'fdp-public-tls',
                    '--cert', str(workdir / 'tls.crt'), '--key', str(workdir / 'tls.key'))
            kubectl('-n', 'fdp', 'create', 'secret', 'generic', 'platform-ca', '--from-file=ca-bundle.pem=' + str(workdir / 'ca.pem'))
            git_server(kubectl, kubeconfig, cluster, workdir)
            step('helm install (bundled profile, no shared NFS volume)')
            started = time.monotonic()
            network, external_db = external_database(cluster, workdir)
            command(['helm', 'install', 'fdp', str(CHART), '-n', 'fdp', '-f', str(values(workdir, cluster, network)),
                     '--kubeconfig', kubeconfig, '--wait', '--timeout', '10m'], timeout=900)
            step(f'Installed in {time.monotonic() - started:.0f}s')
            port, api_port = free_port(), free_port()
            forward = subprocess.Popen(['kubectl', '--kubeconfig', kubeconfig, '-n', 'fdp', 'port-forward',
                                        'svc/fdp-traefik', f'{port}:443', f'{api_port}:9443'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            platform = Platform(kubeconfig, str(workdir / 'ca.pem'), port, api_port)
            platform.workdir = workdir
            platform.external_db = external_db
            platform.proxy_port = https_proxy(port)
            wait(lambda: platform.http.get('https://idp.fdp.test/realms/fdp/.well-known/openid-configuration').status_code == 200
                 if forward.poll() is None else None, 'gateway reachable', 60, 2)
            try:
                scenario(platform, cluster, workdir)
            except Exception:
                # Keep the evidence: the cluster is deleted right after this.
                for target in ('deploy/fdp-control-plane', 'deploy/fdp-layer-controller'):
                    logs = subprocess.run(['kubectl', '--kubeconfig', kubeconfig, '-n', 'fdp', 'logs', target,
                                           '--tail', '120'], capture_output=True, text=True).stdout
                    print(f'----- {target} (last 120 lines) -----\n' + logs, flush=True)
                raise
            step('PASSED: install, OpenTofu, both layers, job, app, Airflow and upgrade verified end to end')
        finally:
            if forward:
                forward.terminate()
            if created:
                step('Deleting the disposable cluster')
                command(['k3d', 'cluster', 'delete', cluster], check=False, timeout=300)
            command(['docker', 'rm', '-f', cluster + '-sales-db'], check=False)
            command(['docker', 'rmi', 'fdp/control-plane:' + cluster, 'fdp/control-plane:' + cluster + '-upgrade'], check=False)


if __name__ == '__main__':
    main()
