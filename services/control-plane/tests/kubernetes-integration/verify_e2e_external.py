"""The external-services profile end to end on a disposable k3s cluster.

The platform is installed with every bundled dependency off; it uses services an organization
already runs, which this test stands up in a separate `external` namespace:

- PostgreSQL for platform metadata (a least-privilege `fdp` role; Keycloak gets its own database),
- OpenBao in server mode (initialized and unsealed here) with a policy-scoped platform token,
- Keycloak as the organization's OIDC provider over HTTPS, with its own realm and clients.

Like an organization's IdP, the test provisions people through the platform's public SCIM endpoint.
The operator-managed platform Secret is written by hand (no bootstrap). Checks: sign-in, OpenTofu
apply with the automation client and an empty re-plan, jobs on both layers' Trino, a secret stored
in OpenBao and nowhere in Kubernetes, then a Helm upgrade that keeps all of it.

    make test-e2e-external
"""
import base64
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_e2e_install as base  # noqa: E402
from verify_e2e_install import (CHART, NON_PROD, PROD, ROOT, Platform, command, free_port, git_repository,  # noqa: E402
                                https_proxy, issue, require, step, tls_material, tofu_runner, wait)

IDP_HOST = 'idp.external.test'
ISSUER = f'https://{IDP_HOST}/realms/fdp'
OPENBAO_IMAGE = 'openbao/openbao:2.1.0'
IMAGES = [*base.IMAGES, OPENBAO_IMAGE]
CORE_USER = 'urn:ietf:params:scim:schemas:core:2.0:User'

OPENBAO_CONFIG = '''storage "file" { path = "/openbao/file" }
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}
api_addr      = "http://openbao.external.svc:8200"
disable_mlock = true
'''
# Exactly what the platform does with its mount: KV v2 data and metadata, nothing else.
OPENBAO_POLICY = '''path "fdp/data/*" { capabilities = ["create", "read", "update", "delete"] }
path "fdp/metadata/*" { capabilities = ["read", "list", "delete"] }
'''


def realm(admin_password, automation_secret):
    audience = {'name': 'api-audience', 'protocol': 'openid-connect', 'protocolMapper': 'oidc-audience-mapper',
                'config': {'included.client.audience': 'fdp-api', 'access.token.claim': 'true', 'id.token.claim': 'false'}}
    return {
        'realm': 'fdp', 'enabled': True, 'accessTokenLifespan': 900,
        'roles': {'realm': [{'name': 'fdp-admin'}]},
        'clients': [
            {'clientId': 'fdp-api', 'enabled': True, 'bearerOnly': True},
            {'clientId': 'fdp-desktop', 'enabled': True, 'publicClient': True, 'standardFlowEnabled': True,
             'directAccessGrantsEnabled': True,  # the test signs in without a browser
             'redirectUris': ['http://127.0.0.1/*'], 'attributes': {'pkce.code.challenge.method': 'S256'},
             'protocolMappers': [audience, {'name': 'groups', 'protocol': 'openid-connect',
                 'protocolMapper': 'oidc-group-membership-mapper',
                 'config': {'claim.name': 'groups', 'full.path': 'true', 'access.token.claim': 'true', 'id.token.claim': 'true'}}]},
            {'clientId': 'fdp-automation', 'enabled': True, 'publicClient': False, 'standardFlowEnabled': False,
             'directAccessGrantsEnabled': False, 'serviceAccountsEnabled': True, 'secret': automation_secret,
             'protocolMappers': [audience]},
        ],
        'users': [
            {'username': 'admin', 'enabled': True, 'email': 'admin@external.test', 'emailVerified': True,
             'firstName': 'Platform', 'lastName': 'Admin', 'realmRoles': ['fdp-admin'],
             'credentials': [{'type': 'password', 'value': admin_password, 'temporary': False}]},
            {'username': 'service-account-fdp-automation', 'enabled': True,
             'serviceAccountClientId': 'fdp-automation', 'realmRoles': ['fdp-admin']},
        ],
    }


def external_services(kubectl, workdir, passwords):
    """PostgreSQL, OpenBao and Keycloak in their own namespace, as an organization would run them."""
    kubectl('create', 'namespace', 'external')
    init_sql = ("CREATE ROLE fdp LOGIN PASSWORD '%(fdp)s';\nCREATE DATABASE fdp OWNER fdp;\n"
                "CREATE ROLE keycloak LOGIN PASSWORD '%(keycloak)s';\nCREATE DATABASE keycloak OWNER keycloak;\n") % passwords
    idp_cert = (workdir / 'idp.pem').read_text() + (workdir / 'ca.pem').read_text()
    labels = lambda name: {'app': name}

    def deployment(name, container, volumes=(), security=None):
        return {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': name, 'namespace': 'external'},
                'spec': {'selector': {'matchLabels': labels(name)}, 'template': {'metadata': {'labels': labels(name)},
                    'spec': {'automountServiceAccountToken': False, **({'securityContext': security} if security else {}),
                             'containers': [{'name': name, 'imagePullPolicy': 'Never', **container}], 'volumes': list(volumes)}}}}

    def service(name, port, target):
        return {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': name, 'namespace': 'external'},
                'spec': {'selector': labels(name), 'ports': [{'port': port, 'targetPort': target}]}}

    items = [
        {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'postgres-init', 'namespace': 'external'},
         'stringData': {'init.sql': init_sql}},
        deployment('postgres', {'image': 'postgres:16-alpine', 'env': [{'name': 'POSTGRES_PASSWORD', 'value': passwords['postgres']}],
                                'volumeMounts': [{'name': 'init', 'mountPath': '/docker-entrypoint-initdb.d'}],
                                'readinessProbe': {'exec': {'command': ['pg_isready', '-U', 'postgres']}, 'periodSeconds': 3}},
                   [{'name': 'init', 'secret': {'secretName': 'postgres-init'}}]),
        service('postgres', 5432, 5432),
        {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'openbao', 'namespace': 'external'},
         'data': {'config.hcl': OPENBAO_CONFIG}},
        {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': 'openbao', 'namespace': 'external'},
         'spec': {'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '1Gi'}}}},
        deployment('openbao', {'image': OPENBAO_IMAGE, 'args': ['server'], 'env': [{'name': 'SKIP_SETCAP', 'value': 'true'}],
                               'volumeMounts': [{'name': 'config', 'mountPath': '/openbao/config'},
                                                {'name': 'data', 'mountPath': '/openbao/file'}]},
                   [{'name': 'config', 'configMap': {'name': 'openbao'}},
                    {'name': 'data', 'persistentVolumeClaim': {'claimName': 'openbao'}}],
                   security={'fsGroup': 1000}),
        service('openbao', 8200, 8200),
        {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'keycloak', 'namespace': 'external'},
         'stringData': {'tls.crt': idp_cert, 'tls.key': (workdir / 'idp.key').read_text(),
                        'realm.json': json.dumps(realm(passwords['admin'], passwords['automation']))}},
        deployment('keycloak', {
            'image': 'quay.io/keycloak/keycloak:26.6.4', 'args': ['start', '--import-realm'],
            'env': [{'name': k, 'value': v} for k, v in {
                'KC_HOSTNAME': f'https://{IDP_HOST}', 'KC_HTTPS_CERTIFICATE_FILE': '/tls/tls.crt',
                'KC_HTTPS_CERTIFICATE_KEY_FILE': '/tls/tls.key', 'KC_HEALTH_ENABLED': 'true', 'KC_DB': 'postgres',
                'KC_DB_URL': 'jdbc:postgresql://postgres.external.svc:5432/keycloak', 'KC_DB_USERNAME': 'keycloak',
                'KC_DB_PASSWORD': passwords['keycloak'], 'KC_BOOTSTRAP_ADMIN_USERNAME': 'bootstrap-admin',
                'KC_BOOTSTRAP_ADMIN_PASSWORD': passwords['keycloak_admin']}.items()],
            'volumeMounts': [{'name': 'tls', 'mountPath': '/tls', 'readOnly': True},
                             {'name': 'realm', 'mountPath': '/opt/keycloak/data/import', 'readOnly': True}],
            'readinessProbe': {'httpGet': {'path': '/health/ready', 'port': 9000, 'scheme': 'HTTPS'}, 'periodSeconds': 5},
            'resources': {'requests': {'cpu': '250m', 'memory': '512Mi'}, 'limits': {'memory': '1536Mi'}}},
            [{'name': 'tls', 'secret': {'secretName': 'keycloak', 'items': [{'key': 'tls.crt', 'path': 'tls.crt'},
                                                                            {'key': 'tls.key', 'path': 'tls.key'}]}},
             {'name': 'realm', 'secret': {'secretName': 'keycloak', 'items': [{'key': 'realm.json', 'path': 'fdp-realm.json'}]}}],
            security={'runAsNonRoot': True, 'runAsUser': 1000, 'fsGroup': 1000}),
        service('keycloak', 443, 8443),
    ]
    kubectl('apply', '-f', '-', input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}))
    for name in ('postgres', 'openbao'):
        kubectl('-n', 'external', 'rollout', 'status', f'deploy/{name}', '--timeout=300s', timeout=330)

    # Initialize and unseal OpenBao, enable the platform's KV mount, and issue a scoped token.
    bao = lambda script, **kw: kubectl('-n', 'external', 'exec', '-i', 'deploy/openbao', '--', 'sh', '-c',
                                       'export BAO_ADDR=http://127.0.0.1:8200; ' + script, **kw)
    # bao status exits 2 while sealed and 1 while it cannot reach the server.
    wait(lambda: 'up' in bao('bao status >/dev/null 2>&1; [ $? -ne 1 ] && echo up', check=False), 'OpenBao listening', 120, 2)
    init = json.loads(bao('bao operator init -key-shares=1 -key-threshold=1 -format=json'))
    bao(f"bao operator unseal {init['unseal_keys_b64'][0]} >/dev/null")
    root = init['root_token']
    bao(f'export BAO_TOKEN={root}; bao secrets enable -path=fdp kv-v2 >/dev/null && '
        'bao policy write fdp - >/dev/null', input=OPENBAO_POLICY)
    token = json.loads(bao(f'export BAO_TOKEN={root}; bao token create -orphan -policy=fdp -ttl=24h -format=json'))
    kubectl('-n', 'external', 'rollout', 'status', 'deploy/keycloak', '--timeout=600s', timeout=630)
    return {'root': root, 'unseal': init['unseal_keys_b64'][0], 'bao': bao}, token['auth']['client_token']


def platform_secret(kubectl, passwords, scim_token, openbao_token):
    """The operator-managed platform Secret (bootstrap is off in this profile)."""
    trino_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    values = {
        'FDP_DATABASE_URL': f"postgresql+psycopg://fdp:{passwords['fdp']}@postgres.external.svc:5432/fdp",
        'FDP_SESSION_SIGNING_KEY': secrets.token_urlsafe(48),
        'FDP_GATEWAY_CONFIG_TOKEN': secrets.token_urlsafe(48),
        'FDP_TRINO_SIGNING_KEY': secrets.token_urlsafe(48),
        'FDP_TRINO_JWT_PRIVATE_KEY': trino_key,
        'FDP_SCIM_TOKEN': scim_token,
        'FDP_OPENBAO_TOKEN': openbao_token,
    }
    kubectl('apply', '-f', '-', input=json.dumps({'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
        'metadata': {'name': 'fdp-control-plane', 'namespace': 'fdp'}, 'stringData': values}))


def values(directory, tag):
    path = directory / 'values.yaml'
    path.write_text(json.dumps({
        'image': {'repository': 'fdp/control-plane', 'tag': tag, 'pullPolicy': 'Never'},
        'publicUrl': 'https://fdp.test', 'apiGateway': {'publicUrl': 'https://api.fdp.test:9443'},
        'tls': {'mode': 'existing'}, 'trust': {'caBundleSecret': 'platform-ca'},
        'existingSecret': 'fdp-control-plane', 'bootstrap': {'enabled': False},
        'metadataDatabase': {'enabled': False}, 'keycloak': {'enabled': False},
        'secretManagement': {'backend': 'openbao'},
        'openbao': {'addr': 'http://openbao.external.svc:8200', 'kvMount': 'fdp'},
        # Tokens carry the public issuer; keys are fetched over the cluster network, verified by the CA bundle.
        'oidc': {'issuer': ISSUER, 'directoryMode': 'scim',
                 'jwksUrl': 'https://keycloak.external.svc/realms/fdp/protocol/openid-connect/certs'},
        'layerController': {'enabled': True}, 'dataVolume': {'enabled': False}, 'trino': {'enabled': False},
        'runtime': {'images': {'computePython': base.RUNTIME_IMAGES[0], 'vscode': base.EDITOR_IMAGE}}}))
    return path


class ExternalPlatform(Platform):
    def __init__(self, kubeconfig, ca, port, api_port, idp_port, passwords, scim_token):
        super().__init__(kubeconfig, ca, port, api_port)
        resolve = socket.getaddrinfo

        def with_idp(host, p, *args, **kwargs):
            if host == IDP_HOST:
                return resolve('127.0.0.1', idp_port, *args, **kwargs)
            return resolve(host, p, *args, **kwargs)
        socket.getaddrinfo = with_idp
        self.password, self.passwords, self.scim_token = passwords['admin'], passwords, scim_token

    def scim(self, user_name):
        """What the organization's IdP does: provision a person or service identity with its role."""
        response = self.http.post('https://fdp.test/api/scim/v2/Users', headers={
            'Authorization': 'Bearer ' + self.scim_token, 'Content-Type': 'application/scim+json'}, json={
            'schemas': [CORE_USER], 'userName': user_name, 'externalId': 'external-idp:' + user_name, 'active': True,
            'roles': [{'value': 'fdp-admin'}]})
        require(response.status_code == 201, f'SCIM provisioning of {user_name} failed: {response.status_code} {response.text[:200]}')

    def sign_in(self):
        for user in ('admin', 'service-account-fdp-automation'):
            self.scim(user)
        self.token = self.login()
        me = self.ok('GET', '/v1/me')
        require(me.get('is_admin'), 'the SCIM-provisioned admin is not a Floren Data Platform administrator')
        return me

    def login(self):
        response = self.http.post(f'{ISSUER}/protocol/openid-connect/token', data={
            'grant_type': 'password', 'client_id': 'fdp-desktop', 'username': 'admin', 'password': self.password})
        require(response.status_code == 200, 'external IdP sign-in failed: ' + response.text[:200])
        return response.json()['access_token']

    def metadata_sql(self, statement):
        return self.kubectl('-n', 'external', 'exec', 'deploy/postgres', '--', 'psql', '-U', 'postgres', '-d', 'fdp',
                            '-Atc', statement).strip()

    def sql_engine_of_last_query(self):
        return self.metadata_sql('SELECT engine_layer FROM sql_queries ORDER BY created_at DESC LIMIT 1')

    def fingerprint(self):
        data = json.loads(self.kubectl('-n', 'fdp', 'get', 'secret', 'fdp-control-plane', '-o', 'jsonpath={.data}'))
        rows = self.metadata_sql('SELECT (SELECT count(*) FROM workloads), (SELECT count(*) FROM runs), '
                                 '(SELECT count(*) FROM directory_principals)')
        return base64.b64encode(json.dumps(data, sort_keys=True).encode()).decode()[-24:], rows


def scenario(p, cluster, workdir, openbao):
    p.sign_in()
    step(f'Signed in with the external IdP ({ISSUER}); SCIM-provisioned admin recognized')
    config = base.TOFU_CONFIG.replace('https://idp.fdp.test/realms/fdp', ISSUER)
    tofu = tofu_runner(p, workdir, client_secret=p.passwords['automation'], config=config)
    tofu('apply', '-auto-approve', '-input=false')
    plan = tofu('plan', '-detailed-exitcode', '-input=false', check=False)
    require(plan.returncode == 0, 'second plan was not empty:\n' + plan.stdout[-3000:])
    ids = json.loads(tofu('output', '-json', 'ids').stdout)
    global PROD, NON_PROD  # created by OpenTofu
    PROD, NON_PROD = ids['prod_layer'], ids['nonprod_layer']
    base.PROD, base.NON_PROD = PROD, NON_PROD  # the shared helpers read them there
    step('OpenTofu applied with the external IdP automation client; immediate re-plan is empty')

    def run_job(workload, layer):
        run = p.ok('POST', f'/v1/workloads/{workload}/runs', json={'args': {}})
        p.finished_run(run['id'])
        require(p.sql_engine_of_last_query() == layer, f'job SQL did not run on the {layer} engine')
        return run
    run = run_job(ids['job'], NON_PROD)
    run_job(ids['job_prod'], PROD)
    step('Jobs ran in Non-Prod and Prod on their layer Trino; metadata in the external PostgreSQL')

    p.ok('PUT', '/v1/admin/service-accounts/e2e-external', json={'layer_id': NON_PROD, 'description': 'external profile'})
    value = 'external-' + secrets.token_urlsafe(12)
    p.ok('PUT', '/v1/secrets/sa/e2e-external/partner-token', json={'kind': 'string', 'value': value})
    stored = p.kubectl('-n', 'external', 'exec', 'deploy/openbao', '--', 'sh', '-c',
                       f"BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN={openbao['root']} "
                       'bao kv get -mount=fdp -format=json env/sa/e2e-external/partner-token')
    require(json.loads(stored)['data']['data'].get('value') == value, 'the secret is not in OpenBao')
    everywhere = p.kubectl('get', 'secrets', '-A', '-o', 'json')
    require(base64.b64encode(value.encode()).decode() not in everywhere, 'the secret value is stored in a Kubernetes Secret')
    step('A platform secret is stored in the external OpenBao (scoped token) and in no Kubernetes Secret')

    # Secret-backend recovery 1: OpenBao restarts sealed. The platform says so, then recovers on unseal.
    bao, root = openbao['bao'], openbao['root']
    write = lambda name: p.api('PUT', f'/v1/secrets/sa/e2e-external/{name}', json={'kind': 'string', 'value': 'v-' + name})
    p.kubectl('-n', 'external', 'delete', 'pod', '-l', 'app=openbao', '--wait=true')
    p.kubectl('-n', 'external', 'rollout', 'status', 'deploy/openbao', '--timeout=180s', timeout=210)
    wait(lambda: 'up' in bao('bao status >/dev/null 2>&1; [ $? -ne 1 ] && echo up', check=False), 'OpenBao restarted', 120, 2)
    # While the pod is replaced the answer is "cannot reach"; once the new server listens, "sealed".
    sealed = wait(lambda: (lambda r: r if 'unseal' in r.text or r.status_code == 200 else None)(write('while-sealed')),
                  'sealed refusal', 120, 3)
    require(sealed.status_code == 503 and 'unseal' in sealed.text and 'v-while-sealed' not in sealed.text,
            f'a sealed OpenBao did not give a clear 503: {sealed.status_code} {sealed.text[:200]}')
    require(p.http.get('https://fdp.test/api/v1/client-config').status_code == 200, 'the platform went down with OpenBao')
    bao(f"bao operator unseal {openbao['unseal']} >/dev/null")
    require(wait(lambda: write('after-unseal').status_code == 200, 'writes after unseal', 60, 3), 'writes did not recover')
    kept = bao(f'BAO_TOKEN={root} bao kv get -mount=fdp -field=value env/sa/e2e-external/partner-token')
    require(kept.strip() == value, 'OpenBao lost a secret across its restart')
    step('OpenBao restarted sealed: the platform answered 503 "unseal", stayed up, and recovered on unseal with data intact')

    # Secret-backend recovery 2: the platform token is revoked; an operator issues and installs a new one.
    old = json.loads(p.kubectl('-n', 'fdp', 'get', 'secret', 'fdp-control-plane', '-o', 'jsonpath={.data}'))
    bao(f"BAO_TOKEN={root} bao token revoke {base64.b64decode(old['FDP_OPENBAO_TOKEN']).decode()} >/dev/null")
    refused = write('revoked')
    require(refused.status_code == 503 and 'token' in refused.text, f'a revoked token was not reported clearly: {refused.text[:200]}')
    new = json.loads(bao(f'BAO_TOKEN={root} bao token create -orphan -policy=fdp -ttl=24h -format=json'))['auth']['client_token']
    p.kubectl('-n', 'fdp', 'patch', 'secret', 'fdp-control-plane', '--type', 'merge', '-p',
              json.dumps({'data': {'FDP_OPENBAO_TOKEN': base64.b64encode(new.encode()).decode()}}))
    p.kubectl('-n', 'fdp', 'rollout', 'restart', 'deploy/fdp-control-plane')
    p.kubectl('-n', 'fdp', 'rollout', 'status', 'deploy/fdp-control-plane', '--timeout=300s', timeout=330)
    require(wait(lambda: write('after-token').status_code == 200, 'writes with the new token', 120, 3), 'new token not used')
    step('Platform token revoked: reported as a token problem; a replacement token restored secret access')

    before = p.fingerprint()
    command(['docker', 'tag', 'fdp/control-plane:' + cluster, 'fdp/control-plane:' + cluster + '-upgrade'])
    command(['k3d', 'image', 'import', '-c', cluster, 'fdp/control-plane:' + cluster + '-upgrade'], timeout=600)
    command(['helm', 'upgrade', 'fdp', str(CHART), '-n', 'fdp', '-f', str(workdir / 'values.yaml'),
             '--set', f'image.tag={cluster}-upgrade', '--kubeconfig', p.kubeconfig, '--wait', '--timeout', '10m'], timeout=900)
    require(p.fingerprint() == before, 'helm upgrade changed the platform Secret or metadata')
    require(p.ok('GET', f"/v1/runs/{run['id']}/outputs/sql.json")['content_base64'] == base64.b64encode(b'[[42]]').decode(),
            'job output lost across the upgrade')
    require(any(x.get('name') == 'partner-token' for x in p.ok('GET', '/v1/secrets', params={'scope': 'sa', 'owner': 'e2e-external'})),
            'the OpenBao secret is not listed after the upgrade')
    run_job(ids['job'], NON_PROD)
    plan = tofu('plan', '-detailed-exitcode', '-input=false', check=False)
    require(plan.returncode == 0, 'configuration drifted across the upgrade:\n' + plan.stdout[-3000:])
    step('helm upgrade kept metadata, the platform Secret, OpenBao secrets and outputs; jobs run; no drift')


def main():
    missing = [i for i in IMAGES if subprocess.run(['docker', 'image', 'inspect', i], capture_output=True).returncode]
    require(not missing, 'missing local images: ' + ', '.join(missing))
    base.preflight()
    cluster = 'dc-ext-' + uuid.uuid4().hex[:6]
    created, forwards = False, []
    passwords = {k: secrets.token_urlsafe(18) for k in ('postgres', 'fdp', 'keycloak', 'keycloak_admin', 'automation')}
    passwords['admin'] = 'E2e-' + secrets.token_urlsafe(18)
    scim_token = secrets.token_urlsafe(48)
    with tempfile.TemporaryDirectory(prefix='fdp-e2e-ext-') as temporary:
        workdir = Path(temporary)
        kubeconfig = str(workdir / 'kubeconfig')
        kubectl = lambda *a, **kw: command(['kubectl', '--kubeconfig', kubeconfig, *a], **kw)
        try:
            step('Building the control plane from the current sources')
            command(['docker', 'build', '-q', '-t', 'fdp/control-plane:' + cluster, str(ROOT / 'services/control-plane')], timeout=900)
            tls_material(workdir)
            from cryptography import x509
            ca = (x509.load_pem_x509_certificate((workdir / 'ca.pem').read_bytes()),
                  serialization.load_pem_private_key((workdir / 'ca.key').read_bytes(), password=None))
            issue(workdir, 'idp', IDP_HOST, issuer=ca, server_names=[IDP_HOST, 'keycloak.external.svc'])
            git_repository(workdir)
            step(f'Creating disposable cluster {cluster}')
            command(['k3d', 'cluster', 'create', cluster, '--agents', '0', '--no-lb', '--kubeconfig-update-default=false',
                     '--kubeconfig-switch-context=false', '--k3s-arg', '--disable=traefik@server:0',
                     '--k3s-arg', '--disable=metrics-server@server:0', '--wait'], timeout=300)
            created = True
            Path(kubeconfig).write_text(command(['k3d', 'kubeconfig', 'get', cluster]))
            step('Importing images')
            for image in ['fdp/control-plane:' + cluster, *IMAGES]:
                started = time.monotonic()
                command(['k3d', 'image', 'import', '-c', cluster, image], timeout=1200)
                step(f'  imported {image} in {time.monotonic() - started:.0f}s')
            step('Starting the external services: PostgreSQL, OpenBao (server mode), Keycloak over HTTPS')
            openbao, openbao_token = external_services(kubectl, workdir, passwords)
            kubectl('create', 'namespace', 'fdp')
            kubectl('-n', 'fdp', 'create', 'secret', 'tls', 'fdp-public-tls',
                    '--cert', str(workdir / 'tls.crt'), '--key', str(workdir / 'tls.key'))
            kubectl('-n', 'fdp', 'create', 'secret', 'generic', 'platform-ca', '--from-file=ca-bundle.pem=' + str(workdir / 'ca.pem'))
            platform_secret(kubectl, passwords, scim_token, openbao_token)
            base.git_server(kubectl, kubeconfig, cluster, workdir)
            step('helm install (external profile: no bundled database, IdP or secret store; no bootstrap)')
            started = time.monotonic()
            command(['helm', 'install', 'fdp', str(CHART), '-n', 'fdp', '-f', str(values(workdir, cluster)),
                     '--kubeconfig', kubeconfig, '--wait', '--timeout', '10m'], timeout=900)
            step(f'Installed in {time.monotonic() - started:.0f}s')
            port, api_port, idp_port = free_port(), free_port(), free_port()
            forwards = [subprocess.Popen(['kubectl', '--kubeconfig', kubeconfig, '-n', 'fdp', 'port-forward',
                                          'svc/fdp-traefik', f'{port}:443', f'{api_port}:9443'],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                        subprocess.Popen(['kubectl', '--kubeconfig', kubeconfig, '-n', 'external', 'port-forward',
                                          'svc/keycloak', f'{idp_port}:443'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
            platform = ExternalPlatform(kubeconfig, str(workdir / 'ca.pem'), port, api_port, idp_port, passwords, scim_token)
            platform.workdir = workdir
            platform.proxy_port = https_proxy(port, {IDP_HOST: idp_port})
            wait(lambda: platform.http.get(f'{ISSUER}/.well-known/openid-configuration').status_code == 200
                 and platform.http.get('https://fdp.test/api/v1/client-config').status_code == 200, 'IdP and gateway reachable', 120, 2)
            try:
                scenario(platform, cluster, workdir, openbao)
            except Exception:
                for target in ('deploy/fdp-control-plane', 'deploy/fdp-layer-controller'):
                    logs = subprocess.run(['kubectl', '--kubeconfig', kubeconfig, '-n', 'fdp', 'logs', target,
                                           '--tail', '120'], capture_output=True, text=True).stdout
                    print(f'----- {target} (last 120 lines) -----\n' + logs, flush=True)
                raise
            step('PASSED: external PostgreSQL, OIDC provider and OpenBao: install, sign-in, OpenTofu, jobs, secrets and upgrade')
        finally:
            for forward in forwards:
                forward.terminate()
            if created:
                step('Deleting the disposable cluster')
                command(['k3d', 'cluster', 'delete', cluster], check=False, timeout=300)
            command(['docker', 'rmi', 'fdp/control-plane:' + cluster, 'fdp/control-plane:' + cluster + '-upgrade'], check=False)


if __name__ == '__main__':
    main()
