"""Acceptance test: OpenTofu with this provider against a real control plane (local, temporary).

For every resource type: create, an empty re-plan, update, import, and destroy; plus drift detection
and repair, and write-only credentials staying out of state. The control plane runs with SQLite, the in-memory secret store
and the fake orchestrator; an OIDC issuer is simulated with a throwaway RSA key. Layer
reconciliation is simulated by marking operations Ready (the real controller has its own tests).
Nothing outside a temporary directory is touched.
"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[3]
PROVIDER = ROOT / 'terraform/provider-fdp'
CONTROL_PLANE = ROOT / 'services/control-plane'
SECRET_MARKERS = ('tf-acceptance-git-token', 'tf-acceptance-db-password', 'tf-acceptance-secret-value', 'PRIVATE KEY')
# After import, write-only credentials cannot be read back: these resend them once on the next apply.
CREDENTIAL_RESOURCES = {'fdp_data_source.warehouse', 'fdp_repository.analytics',
                        'fdp_secret.api_key', 'fdp_secret.partner_ca'}


def step(message):
    print(f'[{time.strftime("%H:%M:%S")}] {message}', flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def run(args, cwd=None, env=None, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args[:3])} failed:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}")
    return result


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Issuer:
    """A minimal OIDC issuer: a JWKS endpoint and tokens for one platform administrator."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        body = json.dumps({'keys': [{**jwk, 'kid': 'acceptance', 'alg': 'RS256', 'use': 'sig'}]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)
        self.port = free_port()
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.issuer = f'http://127.0.0.1:{self.port}/realms/acceptance'

    def token(self, username='tf-admin', admin=True):
        now = int(time.time())
        return jwt.encode({'iss': self.issuer, 'aud': 'fdp-api', 'sub': username, 'preferred_username': username,
                           'realm_access': {'roles': ['fdp-admin'] if admin else []}, 'groups': [], 'iat': now, 'exp': now + 3600},
                          self.key, algorithm='RS256', headers={'kid': 'acceptance'})


class PackageIndex:
    """A tiny PyPI-style simple index with one pure-Python wheel, so imports work without the internet."""
    NAME, VERSION = 'fdp-acceptance-demo', '1.0.0'

    def __init__(self):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr(f'fdp_acceptance_demo-{self.VERSION}.dist-info/METADATA',
                       f'Metadata-Version: 2.1\nName: {self.NAME}\nVersion: {self.VERSION}\nSummary: Acceptance test package\n')
            z.writestr(f'fdp_acceptance_demo-{self.VERSION}.dist-info/WHEEL',
                       'Wheel-Version: 1.0\nGenerator: acceptance\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            z.writestr(f'fdp_acceptance_demo-{self.VERSION}.dist-info/RECORD', '')
            z.writestr('fdp_acceptance_demo/__init__.py', '')
        wheel, filename = buf.getvalue(), f'fdp_acceptance_demo-{self.VERSION}-py3-none-any.whl'
        self.port = free_port()
        index = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.rstrip('/') == f'/simple/{index.NAME}':
                    body, kind = f'<a href="/files/{filename}">{filename}</a>'.encode(), 'text/html'
                elif self.path == f'/files/{filename}':
                    body, kind = wheel, 'application/octet-stream'
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.port}/simple'


def git_repository(directory):
    work, bare = directory / 'work', directory / 'analytics.git'
    (work / 'jobs').mkdir(parents=True)
    (work / 'fdp.json').write_text(json.dumps({'version': 1, 'warehouse': 'tf-small', 'programs': {
        'report': {'description': 'Nightly report', 'entrypoint': 'jobs/report.py', 'warehouse': 'tf-small'}}}))
    (work / 'jobs/report.py').write_text('print("report")\n')
    git = lambda *a, cwd=work: run(['git', *a], cwd=cwd)
    git('init', '-q', '-b', 'main')
    git('add', '.')
    git('-c', 'user.email=tf@test', '-c', 'user.name=tf', 'commit', '-qm', 'programs')
    git('clone', '-q', '--bare', '.', str(bare))
    return bare


def certificate():
    """A self-signed CA: public PEM goes in configuration, the private key only as a write-only value."""
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Partner CA')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())


def config(repo, cert, version=1):
    """The whole configuration; version 2 changes an updatable setting on every resource type."""
    v2 = version == 2
    pem, key = cert
    return f"""
terraform {{
  required_providers {{
    fdp = {{ source = "registry.opentofu.org/floren/fdp" }}
  }}
}}

provider "fdp" {{}}

data "fdp_layer" "nonprod" {{ name = lower(fdp_layer.nonprod.name) }}
data "fdp_principal" "admin" {{ name = "tf-admin" }}

# The platform has no built-in layers: they all come from configuration.
resource "fdp_layer" "nonprod" {{
  name           = "Non-Prod"
  infrastructure = jsonencode({{ enabled = true, trino = {{ coordinator = {{ cpu = {1 if v2 else 0.5}, memory = "1536Mi" }},
                                                           workers = {{ warehouse = "tf-sql", min = 1, max = {3 if v2 else 2} }} }} }})
  timeouts = {{ create = "2m", update = "2m" }}
}}

resource "fdp_layer" "analytics" {{
  name        = "Analytics"
  description = "{'Updated' if v2 else 'Created'} by OpenTofu"
  production  = {'true' if v2 else 'false'}
}}

resource "fdp_default_layer" "this" {{
  layer_id = fdp_layer.nonprod.id
}}

resource "fdp_default_warehouse" "this" {{
  name = fdp_compute_pool.small.name
}}

resource "fdp_compute_pool" "small" {{
  name           = "tf-small"
  cpu            = 1
  memory_gb      = {3 if v2 else 2}
  max_concurrent = {4 if v2 else 3}
}}

resource "fdp_compute_pool" "dedicated" {{
  name           = "tf-data-eng"
  cpu            = 2
  memory_gb      = 8
  dedicated      = true
  allowed_groups = [{'"data-engineers", "platform"' if v2 else '"data-engineers"'}]
}}

resource "fdp_workspace_settings" "this" {{
  warehouse            = fdp_compute_pool.small.name
  max_running          = {5 if v2 else 4}
  max_running_per_user = 2
}}

resource "fdp_query_log_settings" "this" {{
  retention_days = {60 if v2 else 30}
  record_sql     = {'true' if v2 else 'false'}
}}

resource "fdp_package_allowlist" "this" {{
  python = {{
    "fdp-acceptance-demo" = "{'Acceptance package' if v2 else ''}"
    "scikit-*"            = ""
  }}
  r = {{ "DBI" = "" }}
}}

resource "fdp_package" "demo" {{
  kind    = "python"
  name    = "fdp-acceptance-demo"
  version = "1.0.0"
  depends_on = [fdp_package_allowlist.this]
}}

resource "fdp_runtime" "python311" {{
  kind        = "python"
  version     = "3.11"
  image       = "fdp/compute-python:3.11"
  description = "{'Older projects' if v2 else ''}"
}}

resource "fdp_egress_allowlist" "this" {{
  hosts = {{
    "github.com"              = "Git"
    "*.githubusercontent.com" = "{'Raw files and LFS' if v2 else ''}"
  }}
}}

resource "fdp_data_source" "lake" {{
  name        = "lake"
  kind        = "iceberg"
  layer_id    = fdp_layer.nonprod.id
  description = "{'Lakehouse tables' if v2 else ''}"
  config      = jsonencode({{ catalog = "lake", warehouse = "s3://lake/", catalog_uri = "http://iceberg-rest:8181" }})
}}

resource "fdp_data_source" "warehouse" {{
  name     = "warehouse"
  kind     = "postgres"
  layer_id = fdp_layer.nonprod.id
  config   = jsonencode({{ host = "db.internal", port = {5433 if v2 else 5432}, database = "sales", catalog = "warehouse" }})
  provisioner_secret_wo  = {{ username = "governance_admin", password = "tf-acceptance-db-password" }}
  credentials_wo_version = 1
}}

resource "fdp_schema" "reporting" {{
  data_source = fdp_data_source.lake.name
  name        = "reporting"
}}

resource "fdp_app_group" "analysts" {{
  name        = "Analysts"
  description = "{'Finance analysts' if v2 else ''}"
  owners      = [data.fdp_principal.admin.id]
}}

resource "fdp_workspace_template" "analysts" {{
  name           = "{'Analysis' if v2 else 'Exploration'}"
  group_id       = fdp_app_group.analysts.id
  editor         = "{'rstudio' if v2 else 'vscode'}"
  layer_id       = fdp_layer.nonprod.id
  repository_ids = [fdp_repository.analytics.id]
}}

resource "fdp_app_group_member" "admin" {{
  app_group_id = fdp_app_group.analysts.id
  principal_id = data.fdp_principal.admin.id
}}

resource "fdp_role_assignment" "analysts_read" {{
  resource_id  = fdp_schema.reporting.id
  app_group_id = fdp_app_group.analysts.id
  role         = "{'Contributor' if v2 else 'Reader'}"
}}

# The package repository: who publishes and imports (everyone installs).
resource "fdp_role_assignment" "analysts_publish" {{
  resource_id  = "packages"
  app_group_id = fdp_app_group.analysts.id
  role         = "{'Maintainer' if v2 else 'Publisher'}"
}}

resource "fdp_role_assignment" "admin_contribute" {{
  resource_id  = fdp_schema.reporting.id
  principal_id = data.fdp_principal.admin.id
  role         = "Contributor"
}}

resource "fdp_repository" "analytics" {{
  name                   = "analytics"
  url                    = "file://{repo}"
  description            = "{'Analytics programs' if v2 else ''}"
  credentials_wo         = {{ username = "bot", token = "tf-acceptance-git-token" }}
  credentials_wo_version = 1
}}

resource "fdp_folder" "finance" {{
  name     = "{'Finance team' if v2 else 'Finance'}"
  layer_id = fdp_layer.nonprod.id
}}

resource "fdp_service_account" "etl" {{
  name        = "tf-etl"
  layer_id    = fdp_layer.nonprod.id
  description = "{'Nightly ETL' if v2 else 'ETL'}"
}}

resource "fdp_secret" "api_key" {{
  scope            = "sa"
  owner            = fdp_service_account.etl.name
  name             = "partner-key"
  description      = "{'Partner key (rotated)' if v2 else 'Partner key'}"
  value_wo         = "tf-acceptance-secret-value"
  value_wo_version = {2 if v2 else 1}
}}

resource "fdp_secret" "partner_ca" {{
  scope            = "sa"
  owner            = fdp_service_account.etl.name
  name             = "partner-ca"
  kind             = "certificate"
  certificate      = <<-PEM
{pem}PEM
  private_key_wo   = <<-PEM
{key}PEM
  value_wo_version = 1
}}

resource "fdp_workload" "report" {{
  repository_id = fdp_repository.analytics.id
  branch        = "main"
  program       = "report"
  slug          = "nightly-report"
  layer_id      = fdp_layer.nonprod.id
  folder_id     = fdp_folder.finance.id
  warehouse     = fdp_compute_pool.small.name
  env           = {{ REPORT_MODE = "{'delta' if v2 else 'full'}" }}
}}

resource "fdp_api" "orders" {{
  name      = "Orders API"
  slug      = "orders"
  direction = "internal"
  layer_id  = fdp_layer.nonprod.id
  inbound   = jsonencode({{ jwt = {{ enabled = true, issuer = "https://idp.partner.test", audience = "{'orders-v2' if v2 else 'orders'}",
                                     jwks_uri = "https://idp.partner.test/jwks" }} }})
}}

resource "fdp_api_operation" "run_report" {{
  api_id = fdp_api.orders.id
  name   = "run-report"
  method = "POST"
  path   = "/reports"
  config = jsonencode({{ workload_id = fdp_workload.report.id, mode = "{'sync' if v2 else 'async'}" }})
}}

resource "fdp_api" "partner" {{
  name      = "Partner API"
  slug      = "partner"
  direction = "external"
  layer_id  = fdp_layer.nonprod.id
  outbound  = jsonencode({{ base_url = "https://partner.example.com/api", timeout_seconds = {20 if v2 else 30} }})
}}

resource "fdp_api_operation" "orders_list" {{
  api_id = fdp_api.partner.id
  name   = "list-orders"
  path   = "{'/orders/v2' if v2 else '/orders'}"
}}

resource "fdp_catalog_governance" "report" {{
  resource_id    = fdp_workload.report.resource_id
  description    = "{'Nightly sales report, refreshed daily' if v2 else 'Nightly sales report'}"
  domain         = "Finance"
  owner_group_id = fdp_app_group.analysts.id
  steward        = "tf-admin"
  classification = "internal"
  tags           = ["finance", "{'daily' if v2 else 'nightly'}"]
}}

resource "fdp_sla_policy" "report" {{
  resource_id = fdp_workload.report.resource_id
  policy      = jsonencode({{ success_rate_percent = {99 if v2 else 95}, window_hours = 168 }})
}}

resource "fdp_metric" "report_rows" {{
  resource_id = fdp_workload.report.resource_id
  key         = "rows-processed"
  name        = "Rows processed"
  unit        = "rows"
  minimum     = {10 if v2 else 1}
}}
"""


IMPORT_IDS = {
    'fdp_layer': lambda v: v['id'], 'fdp_compute_pool': lambda v: v['name'],
    'fdp_data_source': lambda v: v['name'], 'fdp_schema': lambda v: v['id'],
    'fdp_app_group': lambda v: v['id'], 'fdp_app_group_member': lambda v: v['id'],
    'fdp_role_assignment': lambda v: v['id'], 'fdp_repository': lambda v: v['name'],
    'fdp_folder': lambda v: v['id'], 'fdp_service_account': lambda v: v['name'],
    'fdp_secret': lambda v: f"{v['scope']}/{v['owner']}/{v['name']}", 'fdp_workload': lambda v: v['id'],
    'fdp_api': lambda v: v['id'], 'fdp_api_operation': lambda v: f"{v['api_id']}/{v['id']}",
    'fdp_catalog_governance': lambda v: v['resource_id'], 'fdp_sla_policy': lambda v: v['resource_id'],
    'fdp_metric': lambda v: f"{v['resource_id']}/{v['key']}", 'fdp_workspace_settings': lambda v: 'workspaces',
    'fdp_default_layer': lambda v: 'default', 'fdp_default_warehouse': lambda v: 'default',
    'fdp_workspace_template': lambda v: v['id'], 'fdp_egress_allowlist': lambda v: 'egress', 'fdp_runtime': lambda v: f"{v['kind']}/{v['version']}", 'fdp_query_log_settings': lambda v: 'query_log', 'fdp_package_allowlist': lambda v: 'package_allowlist', 'fdp_package': lambda v: f"{v['kind']}/{v['name']}/{v['version']}",
}


def main():
    with tempfile.TemporaryDirectory(prefix='fdp-tofu-') as temporary:
        work = Path(temporary)
        step('Building the provider')
        plugins = work / 'plugins'
        run(['go', 'build', '-o', str(plugins / 'terraform-provider-fdp'), '.'], cwd=PROVIDER)
        (work / 'tofurc').write_text(f'provider_installation {{\n  dev_overrides {{ "registry.opentofu.org/floren/fdp" = "{plugins}" }}\n  direct {{}}\n}}\n')
        issuer, port, database = Issuer(), free_port(), work / 'fdp.db'
        packages = PackageIndex()
        repo, cert = git_repository(work), certificate()
        env = {**os.environ, 'FDP_DATABASE_URL': f'sqlite:///{database}', 'FDP_ORCHESTRATOR': 'fake',
               'FDP_SECRETS_BACKEND': 'memory', 'FDP_OIDC_ISSUER': issuer.issuer,
               'FDP_OIDC_JWKS_URL': f'http://127.0.0.1:{issuer.port}/jwks', 'FDP_OIDC_AUDIENCE': 'fdp-api',
               'FDP_DIRECTORY_MODE': 'oidc', 'FDP_ALLOW_FILE_REPOS': 'true', 'FDP_SESSION_SIGNING_KEY': 'acceptance-' + 'x' * 40,
               'FDP_PUBLIC_URL': f'http://127.0.0.1:{port}', 'FDP_GIT_CACHE_DIR': str(work / 'git-cache'),
               'FDP_DATA_DIR': str(work / 'data'), 'FDP_PACKAGE_DIR': str(work / 'packages'),
               'FDP_PACKAGE_UPSTREAM_PYPI': packages.url, 'FDP_PACKAGE_IMPORTS_INLINE': 'true',
               'FDP_PACKAGE_PYTHON_VERSIONS': '3.12', 'FDP_PACKAGE_PLATFORMS': 'x86_64'}
        step('Starting a temporary control plane')
        server = subprocess.Popen([str(CONTROL_PLANE / '.venv/bin/python'), '-m', 'uvicorn', 'app.main:app', '--port', str(port)],
                                  cwd=CONTROL_PLANE, env=env, stdout=subprocess.DEVNULL, stderr=open(work / 'server.log', 'w'))
        stop = threading.Event()
        try:
            token = issuer.token()
            api = httpx.Client(base_url=f'http://127.0.0.1:{port}', headers={'Authorization': 'Bearer ' + token}, timeout=30)
            for _ in range(60):
                try:
                    if api.get('/v1/me').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError('control plane did not start:\n' + (work / 'server.log').read_text()[-2000:])

            def controller():  # stands in for the layer controller: accept every queued generation
                while not stop.is_set():
                    try:
                        with sqlite3.connect(database) as db:
                            # A retired layer keeps its storage, as the real controller reports it.
                            db.execute("UPDATE layer_operations SET "
                                       "phase = CASE WHEN json_extract((SELECT config FROM rbac_resources r WHERE r.id = layer_operations.layer_id), "
                                       "'$.infrastructure.retired') THEN 'Retired' ELSE 'Ready' END, "
                                       "reason = CASE WHEN json_extract((SELECT config FROM rbac_resources r WHERE r.id = layer_operations.layer_id), "
                                       "'$.infrastructure.retired') THEN 'StorageRetained' ELSE 'Reconciled' END, message='simulated' "
                                       "WHERE phase IN ('Pending','Provisioning')")
                    except sqlite3.Error:
                        pass
                    stop.wait(2)
            threading.Thread(target=controller, daemon=True).start()

            tofu_env = {**os.environ, 'TF_CLI_CONFIG_FILE': str(work / 'tofurc'), 'FDP_ENDPOINT': f'http://127.0.0.1:{port}',
                        'FDP_TOKEN': token, 'TF_IN_AUTOMATION': '1'}
            tofu = lambda *a, check=True: run(['tofu', *a, '-no-color'], cwd=work, env=tofu_env, check=check)

            def empty_plan(what, allowed=frozenset()):
                tofu('plan', '-input=false', '-out=' + str(work / 'plan.bin'))
                changes = json.loads(run(['tofu', 'show', '-json', str(work / 'plan.bin')], cwd=work, env=tofu_env).stdout)
                changed, detail = set(), []
                for c in changes.get('resource_changes', []):
                    if c['change']['actions'] == ['no-op'] or c['change']['actions'] == ['read']:
                        continue  # reading a data source is not a change
                    before, after = c['change'].get('before') or {}, c['change'].get('after') or {}
                    unknown = c['change'].get('after_unknown') or {}
                    # Timeouts are configuration-only, and values computed during apply are not differences.
                    diff = {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
                            if before.get(k) != after.get(k) and k != 'timeouts' and not unknown.get(k)}
                    if not diff and c['change']['actions'] == ['update']:
                        continue
                    changed.add(c['address'])
                    detail.append(f"  {c['address']} {c['change']['actions']}: {json.dumps(diff, default=str)[:600]}")
                require(changed <= set(allowed), f'{what}: unexpected changes in {sorted(changed - set(allowed))}\n' + '\n'.join(detail))
                return changed

            def resources():
                values = json.loads(tofu('show', '-json').stdout)['values']['root_module']['resources']
                return {r['address']: r for r in values if r['mode'] == 'managed'}

            def secrets_absent():
                state = (work / 'terraform.tfstate').read_text()
                require(not any(marker in state for marker in SECRET_MARKERS), 'a write-only credential was written to state')

            # The initial layer outlives destroy, and its Trino workers keep their warehouse: keep that one outside.
            api.put('/v1/admin/pools/tf-sql', json={'name': 'tf-sql', 'cpu': 1, 'memory_gb': 2}).raise_for_status()
            (work / 'main.tf').write_text(config(repo, cert, 1))
            step('Create: apply every resource type')
            tofu('apply', '-auto-approve', '-input=false')
            secrets_absent()
            created = resources()
            types = sorted({r['type'] for r in created.values()})
            require(len(types) == 26, f'expected 26 resource types, got {types}')
            by_address = {a: r['values'] for a, r in created.items()}
            require(by_address['fdp_layer.nonprod']['phase'] == 'Ready', 'layer did not reach Ready')
            require(by_address['fdp_workload.report']['status'] == 'live', 'workload did not go live')
            require(by_address['fdp_workload.report']['folder_id'] == by_address['fdp_folder.finance']['id'], 'workload not in its folder')
            require(by_address['fdp_secret.api_key']['reference'], 'secret has no reference')
            step(f'Created {len(created)} resources of {len(types)} types; credentials stayed out of state')
            empty_plan('re-plan after create')

            step('Update: change a setting on every resource type')
            (work / 'main.tf').write_text(config(repo, cert, 2))
            tofu('apply', '-auto-approve', '-input=false')
            empty_plan('re-plan after update')
            pools = {p['name']: p for p in api.get('/v1/admin/pools').json()}
            require((pools['tf-small']['memory_gb'], pools['tf-small']['max_concurrent']) == (3, 4), 'pool update not applied')
            require(pools['tf-small']['default'] and pools['tf-sql']['trino_layers'] == ['Non-Prod'],
                    'default warehouse or Trino warehouse not reported')
            require(pools['tf-data-eng']['dedicated'] and pools['tf-data-eng']['allowed_groups'] == ['data-engineers', 'platform'],
                    'dedicated warehouse update not applied')
            limits = api.get('/v1/admin/settings/workspaces').json()
            require((limits['warehouse'], limits['max_running']) == ('tf-small', 5), f'workspace settings not applied: {limits}')
            grants = {a['app_group_id']: a['role'] for a in api.get('/v1/rbac/resources/packages/assignments').json()}
            require(grants.get(by_address['fdp_app_group.analysts']['id']) == 'Maintainer', f'package role not updated: {grants}')
            require(api.get('/v1/admin/settings/query-log').json() == {'retention_days': 60, 'record_sql': True}, 'query log settings not updated')
            allow = api.get('/v1/packages/allowlist').json()
            require(allow['enabled'] and {e['name']: e['description'] for e in allow['python']}['fdp-acceptance-demo'] == 'Acceptance package'
                    and [e['name'] for e in allow['r']] == ['DBI'], f'package allowlist not applied: {allow}')
            demo = api.get('/v1/packages/python/fdp-acceptance-demo').json()
            require(demo['origin'] == 'pypi' and [v['version'] for v in demo['versions']] == ['1.0.0'], f'package not imported: {demo}')
            runtime = next(r for r in api.get('/v1/admin/runtimes').json() if (r['kind'], r['version']) == ('python', '3.11'))
            require(runtime['description'] == 'Older projects' and not runtime['installation'], f'runtime not updated: {runtime}')
            allowlist = api.get('/v1/admin/settings/egress').json()['hosts']
            require({h['host']: h['description'] for h in allowlist} == {'*.githubusercontent.com': 'Raw files and LFS', 'github.com': 'Git'},
                    f'egress allowlist not applied: {allowlist}')
            op = api.get(f"/v1/apis/{by_address['fdp_api.orders']['id']}").json()['operations'][0]
            require(op['config']['mode'] == 'sync', 'operation update not applied')
            secrets_absent()
            step('Updated every resource type; re-plan empty')

            step('Import: forget every resource, import each back by its documented ID')
            current = resources()
            tofu('state', 'rm', *current.keys())
            for address, r in current.items():
                tofu('import', '-input=false', address, IMPORT_IDS[r['type']](r['values']))
            resent = empty_plan('plan after import', allowed=CREDENTIAL_RESOURCES)
            tofu('apply', '-auto-approve', '-input=false')
            empty_plan('plan after re-recording credential versions')
            step(f'Imported {len(current)} resources; only credential holders resent once: {sorted(resent)}')

            step('Drift: change objects outside configuration')
            api.put('/v1/admin/pools/tf-small', json={'name': 'tf-small', 'cpu': 4, 'memory_gb': 3}).raise_for_status()
            orders = by_address['fdp_api.orders']['id']
            body = api.get(f'/v1/apis/{orders}').json()
            api.put(f'/v1/apis/{orders}', json={'name': 'Renamed by hand', 'slug': 'orders', 'direction': 'internal',
                                                'inbound': body['config']['inbound']}).raise_for_status()
            governance = by_address['fdp_catalog_governance.report']['id']
            current_gov = api.get(f'/v1/catalog/assets/{governance}').json()['governance']
            api.put(f'/v1/catalog/assets/{governance}/governance', json={**current_gov, 'domain': 'Marketing'}).raise_for_status()
            drifted = empty_plan('drift', allowed={'fdp_compute_pool.small', 'fdp_api.orders', 'fdp_catalog_governance.report'})
            require(drifted == {'fdp_compute_pool.small', 'fdp_api.orders', 'fdp_catalog_governance.report'},
                    f'drift not detected on every changed object: {sorted(drifted)}')
            tofu('apply', '-auto-approve', '-input=false')
            empty_plan('after repairing drift')
            require(api.get(f'/v1/apis/{orders}').json()['name'] == 'Orders API', 'API name not restored')
            step('Drift on pool, API and governance detected and repaired')

            step('Guardrails: promotion rules and a non-admin automation identity')
            def refused(name, body, expected, token_value=token):
                case = work / name
                case.mkdir()
                (case / 'main.tf').write_text('terraform {\n  required_providers {\n    fdp = { source = "registry.opentofu.org/floren/fdp" }\n  }\n}\n'
                                              'provider "fdp" {}\n' + body)
                result = run(['tofu', 'apply', '-auto-approve', '-input=false', '-no-color'], cwd=case,
                             env={**tofu_env, 'FDP_TOKEN': token_value}, check=False)
                text = result.stdout + result.stderr
                flat = ' '.join(text.split())  # OpenTofu wraps long diagnostics
                require(result.returncode != 0 and expected in flat, f'{name}: expected refusal containing {expected!r}:\n{text[-1500:]}')
                run(['tofu', 'destroy', '-auto-approve', '-input=false', '-no-color'], cwd=case,
                    env={**tofu_env, 'FDP_TOKEN': token_value}, check=False)
            repo_id = resources()['fdp_repository.analytics']['values']['id']
            refused('promotion', f'''
resource "fdp_folder" "release" {{
  name   = "Release"
  branch = "release"
}}
resource "fdp_workload" "wrong_branch" {{
  repository_id = "{repo_id}"
  branch        = "main"
  program       = "report"
  slug          = "report-from-main"
  folder_id     = fdp_folder.release.id
}}
''', 'requires branch release')
            analyst = issuer.token('tf-analyst', admin=False)
            httpx.get(f'http://127.0.0.1:{port}/v1/me', headers={'Authorization': 'Bearer ' + analyst}).raise_for_status()
            refused('scoped-admin-only', '''
resource "fdp_compute_pool" "huge" {
  name      = "tf-huge"
  cpu       = 64
  memory_gb = 256
}
''', '403', analyst)
            other_group = resources()['fdp_app_group.analysts']['values']['id']
            refused('scoped-owner-group', f'''
provider "fdp" {{
  alias          = "scoped"
  owner_group_id = "{other_group}"
}}
resource "fdp_folder" "sneaky" {{
  provider = fdp.scoped
  name     = "Not my group"
}}
''', '403', analyst)
            require('tf-huge' not in {p['name'] for p in api.get('/v1/admin/pools').json()}, 'a non-admin created a pool')
            step('Promotion rule enforced; non-admin automation refused admin-only and foreign-group changes')

            step('Destroy')
            final = {a: r['values'] for a, r in resources().items()}
            tofu('destroy', '-auto-approve', '-input=false')
            layers = {l['name'] for l in api.get('/v1/layers').json()}
            require('Analytics' not in layers and 'Non-Prod' in layers, f'unexpected layers after destroy: {layers}')
            kept = api.get(f"/v1/admin/layers/{final['fdp_layer.nonprod']['id']}/infrastructure").json()
            require(kept['spec']['retired'] and kept['operation']['phase'] == 'Retired',
                    'a layer with infrastructure must be retired, keeping its storage, not deleted')
            require(not api.get('/v1/admin/settings/default-layer').json()['configured'], 'default layer not cleared')
            require('tf-small' not in {p['name'] for p in api.get('/v1/admin/pools').json()}, 'pool not deleted')
            require(api.get('/v1/admin/settings/workspaces').json()['warehouse'] is None, 'workspace settings not reset')
            require(api.get('/v1/admin/settings/egress').json()['hosts'] == [], 'egress allowlist not emptied')
            require(not {'lake', 'warehouse'} & {d['name'] for d in api.get('/v1/admin/datasources').json()}, 'data sources remain')
            require(api.get(f"/v1/rbac/resources/{final['fdp_schema.reporting']['id']}").status_code == 404, 'schema still registered')
            require(api.get(f"/v1/rbac/resources/{final['fdp_folder.finance']['id']}").status_code == 404, 'folder remains')
            require(api.get(f'/v1/apis/{orders}').status_code == 404, 'API remains')
            pub = api.get(f"/v1/workloads/{final['fdp_workload.report']['id']}")
            require(pub.status_code == 404 or pub.json().get('status') == 'stopped', 'workload still running')
            accounts = {a['name']: a for a in api.get('/v1/service-accounts').json()}
            require('tf-etl' not in accounts or accounts['tf-etl']['disabled'], 'service account still enabled')
            require(not [s for s in api.get('/v1/secrets').json() if s['owner'] == 'tf-etl'], 'secrets remain')
            step('PASSED: create, re-plan, update, import, drift repair, guardrails and destroy for all 26 resource types')
        finally:
            stop.set()
            server.terminate()
            server.wait(timeout=20)
            issuer.server.shutdown()


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, RuntimeError) as exc:
        print('FAILED:', exc, file=sys.stderr)
        sys.exit(1)
