"""A local Kubernetes lab for trying Floren Data Platform by hand: Helm install + OpenTofu configuration.

    make lab-up        create the k3d cluster "fdp-lab", install the chart, apply the OpenTofu config
    make lab-status    what is running, and how to sign in
    make lab-tofu ARGS="plan"   run OpenTofu against the lab (the config lives in .lab/tofu/main.tf)
    make lab-down      delete the cluster and everything in .lab/

Same path as a real installation, sized for a laptop and seeded like the end-to-end test: both layers
isolated (Trino, PostgreSQL, RustFS, Iceberg), a warehouse, data sources and schemas, a Git repository
with sample programs, a job deployed in each layer, an app and an Airflow pipeline. Uses only its own
cluster and kubeconfig (.lab/kubeconfig), never your current kubectl context. Secrets stay in .lab/
(gitignored). The test CA and certificates last 7 days; recreate the lab after that.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/control-plane/tests/kubernetes-integration'))
import verify_e2e_install as base  # noqa: E402

LAB = ROOT / '.lab'
CLUSTER = 'fdp-lab'
IMAGE = 'fdp/control-plane:' + CLUSTER
HOSTS = '127.0.0.1 fdp.test api.fdp.test idp.fdp.test'
# Every editor and runtime, so the lab has all features (build them with `make images`).
FEATURE_IMAGES = {'vscode': 'fdp/workspace-vscode:dev', 'rstudio': 'fdp/workspace-rstudio:dev',
                  'sql': 'fdp/workspace-sql:dev', 'computeR': 'fdp/compute-r:dev', 'computeDbt': 'fdp/compute-dbt:1.9',
                  'airflow': 'fdp/airflow:dev', 'airflowBroker': 'valkey/valkey:8.0.1'}


def say(message):
    print(message, flush=True)


def run(args, **kwargs):
    return base.command(args, **kwargs)


def kubectl(*args, **kwargs):
    return run(['kubectl', '--kubeconfig', str(LAB / 'kubeconfig'), *args], **kwargs)


def exists():
    listed = subprocess.run(['k3d', 'cluster', 'list', '-o', 'json'], capture_output=True, text=True)
    return listed.returncode == 0 and any(c['name'] == CLUSTER for c in json.loads(listed.stdout or '[]'))


def free():
    """GB free on Docker's disk."""
    return int(run(['docker', 'run', '--rm', 'alpine:3.20', 'df', '-k', '/']).splitlines()[-1].split()[3]) / 2**20


def present(image):
    return subprocess.run(['docker', 'image', 'inspect', image], capture_output=True).returncode == 0


class Lab:
    """What base.tofu_runner needs: kubectl and the CONNECT proxy (so no hosts entries are required)."""

    def __init__(self):
        self.proxy_port = base.https_proxy(443)

    @staticmethod
    def kubectl(*args, **kwargs):
        return kubectl(*args, **kwargs)


def client_secret():
    import base64
    return base64.b64decode(kubectl('-n', 'fdp', 'get', 'secret', 'fdp-keycloak', '-o',
                                    'jsonpath={.data.automation-client-secret}')).decode()


def tofu_env(lab):
    return {**os.environ, 'TF_CLI_CONFIG_FILE': str(LAB / 'tofurc'), 'TF_IN_AUTOMATION': '1',
            'HTTPS_PROXY': f'http://127.0.0.1:{lab.proxy_port}', 'FDP_CLIENT_SECRET': client_secret()}


def up():
    if exists():
        say('The lab is already running. `make lab-status` shows how to use it; `make lab-down` removes it.')
        return
    shutil.rmtree(LAB, ignore_errors=True)
    LAB.mkdir()
    images = list(dict.fromkeys([*base.IMAGES, *FEATURE_IMAGES.values()]))
    missing = [i for i in images if not present(i)]
    if missing:
        raise SystemExit('missing local images (run `make images`): ' + ', '.join(missing))
    if free() < 18:
        raise SystemExit(f'only {free():.1f} GB free in Docker; the lab needs about 18 GB '
                         '(its node keeps its own copy of every image, and Kubernetes needs headroom)')

    say('Building the control plane from the current sources')
    run(['docker', 'build', '-q', '-t', IMAGE, str(ROOT / 'services/control-plane')], timeout=900)
    base.tls_material(LAB)
    base.git_repository(LAB)
    say(f'Creating the k3d cluster {CLUSTER} (gateway on ports 443 and 9443)')
    run(['k3d', 'cluster', 'create', CLUSTER, '--agents', '0', '-p', '443:443@loadbalancer', '-p', '9443:9443@loadbalancer',
         '--kubeconfig-update-default=false', '--kubeconfig-switch-context=false',
         '--k3s-arg', '--disable=traefik@server:0', '--k3s-arg', '--disable=metrics-server@server:0', '--wait'], timeout=300)
    (LAB / 'kubeconfig').write_text(run(['k3d', 'kubeconfig', 'get', CLUSTER]))
    imported = []
    for image in [IMAGE, *images]:
        size = int(run(['docker', 'image', 'inspect', image, '--format', '{{.Size}}']).strip()) / 2**30
        if free() - 2 * size < 4:  # the node stores it twice while importing
            raise SystemExit(f'not enough Docker disk to load {image} ({free():.1f} GB free); '
                             'raise Docker Desktop\'s disk limit or free space, then `make lab-down` and `make lab-up`')
        started = time.monotonic()
        run(['k3d', 'image', 'import', '-c', CLUSTER, image], timeout=1200)
        imported.append(image)
        say(f'  imported {image} in {time.monotonic() - started:.0f}s')
        if os.environ.get('FDP_LAB_DROP_IMPORTED') == '1' and image.startswith('fdp/'):
            # Small disks: the node keeps its copy; this repository's images rebuild with `make images`.
            run(['docker', 'rmi', image], check=False)
    network, (db_address, db_engine, db_admin) = base.external_database(CLUSTER, LAB)
    (LAB / 'sales-db.json').write_text(json.dumps({'host': db_address, 'engine_user': 'trino_engine',
        'engine_password': db_engine, 'admin_user': 'postgres', 'admin_password': db_admin, 'database': 'sales'}, indent=2))

    kubectl('create', 'namespace', 'fdp')
    kubectl('-n', 'fdp', 'create', 'secret', 'tls', 'fdp-public-tls',
            '--cert', str(LAB / 'tls.crt'), '--key', str(LAB / 'tls.key'))
    kubectl('-n', 'fdp', 'create', 'secret', 'generic', 'platform-ca', '--from-file=ca-bundle.pem=' + str(LAB / 'ca.pem'))
    base.git_server(lambda *a, **kw: kubectl(*a, **kw), str(LAB / 'kubeconfig'), CLUSTER, LAB)
    values_path = base.values(LAB, CLUSTER, network)
    values = json.loads(values_path.read_text())
    values['keycloak']['passwordGrantEnabled'] = False  # people sign in through the browser
    values['keycloak']['desktopRedirectUris'] = ['http://127.0.0.1/*', 'tauri://localhost/*', 'http://tauri.localhost/*',
                                                 'https://localhost:1420/*']  # `make desktop` / `make desktop-web`
    values['runtime']['images'].update(FEATURE_IMAGES)
    values['certificateMonitoring'] = {'intervalSeconds': 600}
    values['egressProxy'] = {'enabled': True}  # workspaces reach allowlisted hosts only
    values_path.write_text(json.dumps(values, indent=2))
    say('helm install')
    run(['helm', 'install', 'fdp', str(base.CHART), '-n', 'fdp', '-f', str(values_path),
         '--kubeconfig', str(LAB / 'kubeconfig'), '--wait', '--timeout', '10m'], timeout=900)

    say('OpenTofu: layers, warehouses, data sources, schemas, grants, repository and deployments')
    lab = Lab()
    tofu = base.tofu_runner(lab, LAB, client_secret=client_secret())
    deadline = time.monotonic() + 900
    while True:  # the directory sync provisions the admin and the automation identity within a minute or two
        result = tofu('apply', '-auto-approve', '-input=false', check=False)
        if result.returncode == 0:
            break
        if time.monotonic() > deadline:
            raise SystemExit('tofu apply failed:\n' + result.stdout[-3000:] + result.stderr[-2000:])
        time.sleep(20)
    seed()
    status()


SEED = {
    'lake': ['CREATE SCHEMA IF NOT EXISTS lake.reporting',
             'CREATE TABLE IF NOT EXISTS lake.reporting.customers (customer_id bigint, name varchar, region varchar, '
             'segment varchar, created_at timestamp(6))',
             "INSERT INTO lake.reporting.customers VALUES (1, 'Acme Retail', 'North', 'Enterprise', TIMESTAMP '2026-01-05 09:00:00'), "
             "(2, 'Bolt Logistics', 'South', 'Mid-market', TIMESTAMP '2026-02-11 14:30:00'), "
             "(3, 'Cedar Foods', 'East', 'SMB', TIMESTAMP '2026-03-20 08:15:00')",
             'CREATE TABLE IF NOT EXISTS lake.reporting.orders (order_id bigint, customer_id bigint, amount decimal(12,2), '
             'status varchar, ordered_at timestamp(6)) WITH (partitioning = ARRAY[\'month(ordered_at)\'])',
             "INSERT INTO lake.reporting.orders VALUES (1001, 1, 1250.00, 'shipped', TIMESTAMP '2026-09-01 10:00:00'), "
             "(1002, 2, 310.50, 'open', TIMESTAMP '2026-09-14 16:20:00'), (1003, 1, 89.99, 'returned', TIMESTAMP '2026-10-02 11:45:00'), "
             "(1004, 3, 4520.00, 'shipped', TIMESTAMP '2026-10-15 09:30:00')"],
    'lake-prod': ['CREATE SCHEMA IF NOT EXISTS lake_prod.reporting',
                  'CREATE TABLE IF NOT EXISTS lake_prod.reporting.revenue_daily (day date, region varchar, revenue decimal(14,2))',
                  "INSERT INTO lake_prod.reporting.revenue_daily VALUES (DATE '2026-10-20', 'North', 18250.40), "
                  "(DATE '2026-10-20', 'South', 9420.00), (DATE '2026-10-21', 'North', 20110.75)"],
}


def seed():
    """Sample Iceberg tables in both layers, written through the governed SQL path as the automation identity."""

    platform = base.Platform(str(LAB / 'kubeconfig'), str(LAB / 'ca.pem'), 443, 9443)
    token = platform.http.post('https://idp.fdp.test/realms/fdp/protocol/openid-connect/token', data={
        'grant_type': 'client_credentials', 'client_id': 'fdp-automation', 'client_secret': client_secret()})
    token.raise_for_status()
    platform.token = token.json()['access_token']
    for source, statements in SEED.items():
        for statement in statements:
            if 'INSERT' in statement:
                table = statement.split()[2]
                count = platform.api('POST', '/v1/sql/query', json={'source': source, 'sql': f'SELECT count(*) FROM {table}'})
                if count.status_code == 200 and count.json().get('rows', [[0]])[0][0]:
                    continue  # already seeded
            response = platform.api('POST', '/v1/sql/query', json={'source': source, 'sql': statement})
            if response.status_code != 200:
                raise SystemExit(f'seeding {source} failed on: {statement[:80]}\n{response.status_code} {response.text[:300]}')
    say('Seeded Iceberg tables: lake.reporting.customers and .orders (Non-Prod), lake_prod.reporting.revenue_daily (Prod)')



def status():
    if not exists():
        raise SystemExit('The lab is not running; `make lab-up` creates it.')
    import base64
    password = base64.b64decode(kubectl('-n', 'fdp', 'get', 'secret', 'fdp-keycloak', '-o',
                                        'jsonpath={.data.initial-user-password}')).decode()
    pods = kubectl('get', 'pods', '-A', '--no-headers')
    total = len(pods.splitlines())
    waiting = [line.split()[:2] for line in pods.splitlines() if 'Running' not in line and 'Completed' not in line]
    say(f'''
Floren Data Platform lab is running ({total} pods{', not ready: ' + str(waiting) if waiting else ', all running'}).

One-time setup on this Mac (needs your password):
  1. Names:  echo "{HOSTS}" | sudo tee -a /etc/hosts
  2. Trust the lab CA (valid 7 days):
       sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain {LAB / "ca.pem"}
     Remove it later with: sudo security remove-trusted-cert -d {LAB / "ca.pem"}

Sign in
  Desktop app:  make desktop   (or make desktop-web), server https://fdp.test
  User admin, initial password (you choose a new one at first sign-in):  {password}
  Keycloak admin console: https://idp.fdp.test

Kubernetes:  export KUBECONFIG={LAB / "kubeconfig"}   (never your default context)
OpenTofu:    edit {LAB / "tofu/main.tf"}, then  make lab-tofu ARGS="plan"  /  ARGS="apply"
External database (to register as a data source yourself): {LAB / "sales-db.json"}
Remove everything: make lab-down
''')


def tofu_command(args):
    if not exists():
        raise SystemExit('The lab is not running; `make lab-up` creates it.')
    lab = Lab()
    result = subprocess.run(['tofu', *args], cwd=LAB / 'tofu', env=tofu_env(lab))
    raise SystemExit(result.returncode)


def down():
    if exists():
        say(f'Deleting the k3d cluster {CLUSTER}')
        run(['k3d', 'cluster', 'delete', CLUSTER], check=False, timeout=300)
    run(['docker', 'rm', '-f', CLUSTER + '-sales-db'], check=False)
    run(['docker', 'rmi', IMAGE], check=False)
    shutil.rmtree(LAB, ignore_errors=True)
    say('Lab removed. If you trusted its CA: sudo security remove-trusted-cert -d <the old .lab/ca.pem> '
        '(or delete "Floren Data Platform e2e root" in Keychain Access), and remove the fdp.test line from /etc/hosts.')


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    {'up': up, 'status': status, 'down': down, 'seed': seed}.get(action, lambda: tofu_command(sys.argv[2:]))()
