"""Validate rendered Kubernetes configuration, including embedded gateway YAML."""
from pathlib import Path
import base64
import json
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / 'deploy/helm/fdp'
pytestmark = pytest.mark.skipif(not shutil.which('helm'), reason='Helm is required for chart rendering checks')


def render(*args):
    result = subprocess.run(['helm','template','fdp',str(CHART),'-n','fdp',*args],
                            capture_output=True,text=True,check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def resource(docs, kind, name):
    return next(doc for doc in docs if doc['kind']==kind and doc['metadata']['name']==name)


def test_kafka_operator_is_opt_in_and_uses_restricted_operand_security():
    assert not any(d['kind'] in ('Kafka','KafkaNodePool') for d in render())
    assert not any(d['kind']=='Deployment' and d['metadata']['name']=='strimzi-cluster-operator' for d in render())
    docs=render('--set','kafkaOperator.enabled=true','--set','layerController.enabled=true')
    operator=resource(docs,'Deployment','strimzi-cluster-operator')
    env={v['name']:v for v in operator['spec']['template']['spec']['containers'][0]['env']}
    assert env['STRIMZI_POD_SECURITY_PROVIDER_CLASS']['value']=='io.strimzi.plugin.security.profiles.impl.RestrictedPodSecurityProvider'
    assert not any(d['kind'] in ('Kafka','KafkaNodePool') for d in docs), 'Installing the operator must not start brokers'


def test_control_plane_startup_and_readiness_use_metadata_checks_but_liveness_does_not():
    for docs in (render(), bundled()):
        container = resource(docs, 'Deployment', 'fdp-control-plane')['spec']['template']['spec']['containers'][0]
        for key in ('startupProbe', 'readinessProbe'):
            assert container[key]['httpGet'] == {'path': '/readyz', 'port': 'http'}
        assert container['livenessProbe']['httpGet'] == {'path': '/healthz', 'port': 'http'}
        assert container['startupProbe']['failureThreshold'] * container['startupProbe']['periodSeconds'] >= 600


@pytest.mark.parametrize('profile', ['bundled.yaml', 'external-services.yaml'])
def test_platform_packages_gateway_controller_and_retained_shared_persistence(profile):
    docs = render('-f', str(CHART/'examples'/profile), '--set', 'layerController.enabled=true')
    pod = resource(docs, 'Deployment', 'fdp-control-plane')['spec']['template']['spec']
    assert resource(docs, 'Deployment', 'fdp-layer-controller')
    assert resource(docs, 'Deployment', 'fdp-traefik')
    assert resource(docs, 'Service', 'fdp-control-plane')['spec']['ports'][0]['port'] == 8000
    assert next(v for v in pod['volumes'] if v['name'] == 'data')['persistentVolumeClaim']['claimName'] == 'fdp-data'
    shares = []
    for namespace in ('fdp', 'fdp-runtime'):
        pv = resource(docs, 'PersistentVolume', 'fdp-data-' + namespace)
        pvc = next(d for d in docs if d['kind'] == 'PersistentVolumeClaim'
                   and d['metadata']['name'] == 'fdp-data' and d['metadata']['namespace'] == namespace)
        assert pv['spec']['claimRef'] == {'namespace': namespace, 'name': 'fdp-data'}
        assert pvc['spec']['volumeName'] == pv['metadata']['name']
        assert pv['spec']['persistentVolumeReclaimPolicy'] == 'Retain'
        assert pv['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
        assert pvc['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
        shares.append(pv['spec']['nfs'])
    assert shares[0] == shares[1]


@pytest.mark.parametrize('setting', ['dataVolume.nfs.server=', 'dataVolume.nfs.path=relative',
                                    'dataVolume.size=invalid', 'runtime.namespace=fdp',
                                    'runtime.namespace=InvalidNamespace'])
def test_invalid_platform_storage_or_namespace_is_rejected_before_install(setting):
    with pytest.raises(subprocess.CalledProcessError):
        render('--set', setting)


def test_layer_network_boundaries_are_controller_only_configuration():
    docs = render('--set', 'layerController.enabled=true',
                  '--set-json', 'layerController.network.podCidrs=["10.42.0.0/16","fd42::/64"]',
                  '--set-json', 'layerController.network.serviceCidrs=["10.43.0.0/16"]',
                  '--set-json', 'layerController.network.nodeCidrs=["192.168.1.0/24"]',
                  '--set-json', 'layerController.network.protectedCidrs=["172.20.5.10/32"]')
    container = resource(docs, 'Deployment', 'fdp-layer-controller')['spec']['template']['spec']['containers'][0]
    env = {entry['name']: entry.get('value') for entry in container['env']}
    assert json.loads(env['FDP_LAYER_POD_CIDRS']) == ['10.42.0.0/16', 'fd42::/64']
    assert json.loads(env['FDP_LAYER_SERVICE_CIDRS']) == ['10.43.0.0/16']
    assert json.loads(env['FDP_LAYER_NODE_CIDRS']) == ['192.168.1.0/24']
    assert json.loads(env['FDP_LAYER_PROTECTED_CIDRS']) == ['172.20.5.10/32']
    roles = [doc for doc in docs if doc['kind'] == 'ClusterRole']
    controller = next(role for role in roles if role['metadata']['name'].endswith('-controller'))
    runtime = next(role for role in roles if role['metadata']['name'].endswith('-runtime'))
    assert [rule['verbs'] for rule in controller['rules'] if rule['resources'] == ['networkpolicies']] == [['get', 'list', 'create', 'patch']]
    assert all('networkpolicies' not in rule['resources'] for rule in runtime['rules'])


def test_layer_controller_is_opt_in_and_runtime_has_no_cluster_binding():
    assert not any(doc['kind'] == 'Deployment' and doc['metadata']['name'] == 'fdp-layer-controller' for doc in render())
    docs = bundled('--set', 'layerController.enabled=true')
    deployment = resource(docs, 'Deployment', 'fdp-layer-controller')
    pod = deployment['spec']['template']['spec']
    assert pod['serviceAccountName'] == 'fdp-layer-controller'
    container = pod['containers'][0]
    assert container['command'] == ['python', '-m', 'app.layer_controller']
    assert 'envFrom' not in container
    env = {v['name']: v for v in container['env']}
    assert env['FDP_DATABASE_PASSWORD']['valueFrom']['secretKeyRef']['key'] == 'fdp-password'
    runtime_role = env['FDP_LAYER_RUNTIME_CLUSTER_ROLE']['value']
    assert not any(d['kind'] == 'ClusterRoleBinding' and d['roleRef']['name'] == runtime_role for d in docs)
    runtime = resource(docs, 'ClusterRole', runtime_role)
    assert all('namespaces' not in r['resources'] and 'networkpolicies' not in r['resources'] for r in runtime['rules'])
    assert all('delete' not in r['verbs'] for r in runtime['rules'] if 'persistentvolumeclaims' in r['resources'])
    binding = next(d for d in docs if d['kind'] == 'ClusterRoleBinding')
    assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': 'fdp-layer-controller', 'namespace': 'fdp'}]
    privileged = resource(docs, 'ClusterRole', binding['roleRef']['name'])
    assert all('*' not in r['resources'] and ('delete' not in r['verbs'] or
        (r['apiGroups'] == ['kafka.strimzi.io'] and r['resources'] == ['kafkatopics'])) for r in privileged['rules'])
    # Generated layer node secrets are create-only: the controller can never read one back.
    assert all(r['verbs'] == ['create'] for r in privileged['rules'] if 'secrets' in r['resources'])
    assert [r for r in privileged['rules'] if 'bind' in r['verbs']] == [{
        'apiGroups': ['rbac.authorization.k8s.io'], 'resources': ['clusterroles'],
        'resourceNames': [runtime_role], 'verbs': ['bind']}]
    db_policy = resource(docs, 'NetworkPolicy', 'fdp-metadata-db-ingress')
    assert {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'fdp'}},
            'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'fdp-layer-controller'}}} in db_policy['spec']['ingress'][0]['from']


def test_layer_controller_external_database_uses_only_database_secret_key():
    docs = render('--set', 'layerController.enabled=true', '--set', 'metadataDatabase.connectionSecret=external-db')
    container = resource(docs, 'Deployment', 'fdp-layer-controller')['spec']['template']['spec']['containers'][0]
    assert next(e for e in container['env'] if e['name'] == 'FDP_DATABASE_URL')['valueFrom']['secretKeyRef'] == {
        'name': 'external-db', 'key': 'database-url'}


@pytest.mark.parametrize('profile,issued', [('tls-existing.yaml',False),('tls-cert-manager.yaml',True)])
def test_public_tls_certificate_and_gateway_configuration_are_consistent(profile, issued):
    docs=render('-f',str(CHART/'examples'/profile))
    config=resource(docs,'ConfigMap','fdp-gateway-tls')['data']['config.yml']
    assert '{{' not in config
    tls=yaml.safe_load(config)['tls']
    pair=tls['stores']['default']['defaultCertificate']
    assert pair=={'certFile':'/etc/traefik/dynamic/tls.crt','keyFile':'/etc/traefik/dynamic/tls.key'}
    assert tls['options']['api-gateway']['clientAuth']['clientAuthType']=='RequestClientCert'
    gateway=resource(docs,'Deployment','fdp-traefik')['spec']['template']['spec']
    projection=next(v for v in gateway['volumes'] if v['name']=='gateway-dynamic')['projected']
    assert any(s.get('secret',{}).get('name')=='fdp-public-tls' for s in projection['sources'])
    assert '--providers.file.watch=true' in gateway['containers'][0]['args']
    assert bool([d for d in docs if d['kind']=='Certificate'])==issued
    if issued:
        spec=resource(docs,'Certificate','fdp-public')['spec']
        assert spec['secretName']=='fdp-public-tls'
        assert spec['dnsNames']==['fdp.example.com','api.fdp.example.com']
        assert spec['issuerRef']['name']=='production-acme'
        assert spec['privateKey']['rotationPolicy']=='Always'


def test_disabled_tls_and_private_trust_do_not_embed_or_require_private_keys():
    docs=render('--set','trust.caBundleSecret=private-ca')
    assert not any(d['kind']=='Certificate' for d in docs)
    tls=yaml.safe_load(resource(docs,'ConfigMap','fdp-gateway-tls')['data']['config.yml'])['tls']
    assert 'certificates' not in tls and 'stores' not in tls
    cp=resource(docs,'Deployment','fdp-control-plane')['spec']['template']['spec']
    env={item['name']:item.get('value') for item in cp['containers'][0]['env']}
    assert env['SSL_CERT_FILE']==env['REQUESTS_CA_BUNDLE']=='/etc/fdp/trust/ca-bundle.pem'
    trust=next(v for v in cp['volumes'] if v['name']=='trust')['secret']
    assert trust['secretName']=='private-ca' and trust['items']==[{'key':'ca-bundle.pem','path':'ca-bundle.pem'}]


@pytest.mark.parametrize('args',[
    ['--set','tls.mode=invalid'],
    ['--set','tls.mode=certManager'],
    ['--set','tls.mode=existing','--set','traefik.enabled=false'],
    ['--set','tls.mode=certManager','--set','tls.issuerRef.name=issuer','--set','tls.renewBefore=3000h'],
])
def test_invalid_certificate_configuration_fails_before_deployment(args):
    with pytest.raises(subprocess.CalledProcessError):
        render(*args)


def test_bundled_metadata_uses_retained_secrets_and_separate_bootstrap_credentials():
    docs=render('-f',str(CHART/'examples/bundled-metadata.yaml'))
    secret=resource(docs,'Secret','fdp-metadata-db')
    assert secret['metadata']['annotations']['helm.sh/resource-policy']=='keep'
    assert set(secret['data'])=={'postgres-password','fdp-password'}
    assert secret['data']['postgres-password']!=secret['data']['fdp-password']
    pvc=resource(docs,'PersistentVolumeClaim','fdp-metadata-db')
    assert pvc['metadata']['annotations']['helm.sh/resource-policy']=='keep'
    db=resource(docs,'StatefulSet','fdp-metadata-db')['spec']['template']['spec']
    assert db['automountServiceAccountToken'] is False
    cp=resource(docs,'Deployment','fdp-control-plane')['spec']['template']['spec']['containers'][0]
    env={v['name']:v for v in cp['env']}
    assert env['FDP_DATABASE_PASSWORD']['valueFrom']['secretKeyRef']['key']=='fdp-password'
    assert not any('postgres-password' in str(v) for v in cp['env'])
    job=resource(docs,'Job','fdp-metadata-bootstrap-1')['spec']['template']['spec']
    assert job['automountServiceAccountToken'] is False
    script=resource(docs,'ConfigMap','fdp-metadata-bootstrap')['data']['init.sql']
    assert '\\getenv app_password APP_PASSWORD' in script
    assert 'CREATE ROLE fdp LOGIN PASSWORD %L' in script
    assert 'SUPERUSER' not in script and 'CREATEROLE' not in script


def test_external_metadata_secret_is_referenced_and_external_database_remains_supported():
    external=render('--set','metadataDatabase.enabled=true','--set','metadataDatabase.existingSecret=managed-db-passwords')
    assert not any(d['kind']=='Secret' and d['metadata']['name']=='fdp-metadata-db' for d in external)
    cp=resource(external,'Deployment','fdp-control-plane')['spec']['template']['spec']['containers'][0]
    password=next(e for e in cp['env'] if e['name']=='FDP_DATABASE_PASSWORD')
    assert password['valueFrom']['secretKeyRef']['name']=='managed-db-passwords'
    default=render()
    assert not any(d['kind']=='StatefulSet' and d['metadata']['name']=='fdp-metadata-db' for d in default)


def bundled(*args):
    return render('-f',str(CHART/'examples/bundled.yaml'),*args)


def test_bundled_platform_keys_match_trino_and_secret_store_has_separate_namespace():
    docs=bundled('--set','openbao.enabled=false','--set','secretManagement.backend=kubernetes')
    cp=resource(docs,'Secret','fdp-control-plane')
    trino=resource(docs,'Secret','fdp-trino')
    assert base64.b64decode(cp['data']['FDP_TRINO_SIGNING_KEY'])==base64.b64decode(base64.b64decode(trino['data']['gateway.hmac']))
    for value in cp['data'].values():assert len(base64.b64decode(value))>=48
    assert cp['metadata']['annotations']['helm.sh/resource-policy']=='keep'
    binding=resource(docs,'RoleBinding','fdp-secret-store')
    assert binding['metadata']['namespace']=='fdp-secrets'
    assert binding['subjects']==[{'kind':'ServiceAccount','name':'fdp-control-plane','namespace':'fdp'}]
    role=resource(docs,'Role','fdp-secret-store')
    assert role['rules']==[{'apiGroups':[''],'resources':['secrets'],'verbs':['get','create','update','delete']}]
    assert resource(docs,'Namespace','fdp-secrets')['metadata']['annotations']['helm.sh/resource-policy']=='keep'
    assert not any(d['kind'] in ('ClusterRole','ClusterRoleBinding') for d in docs)
    bundled_openbao=[d for d in bundled() if d['kind'] in ('ClusterRole','ClusterRoleBinding')]
    assert [(d['kind'],d['roleRef']['name']) for d in bundled_openbao]==[('ClusterRoleBinding','system:auth-delegator')]


def test_bundled_keycloak_scim_and_database_credentials_are_scoped_and_have_no_inline_values():
    docs=bundled('-f',str(CHART/'examples/tls-cert-manager.yaml'))
    realm=json.loads(resource(docs,'ConfigMap','fdp-keycloak-realm')['data']['fdp-realm.json'])
    desktop=next(c for c in realm['clients'] if c['clientId']=='fdp-desktop')
    assert desktop['attributes']['pkce.code.challenge.method']=='S256'
    assert desktop['directAccessGrantsEnabled'] is False
    assert next(u for u in realm['users'] if u['username']=='admin')['credentials'][0]['temporary'] is True
    assert all('${' in c['value'] for u in realm['users'] for c in u.get('credentials',[]))
    assert 'idp.example.com' in resource(docs,'Certificate','fdp-public')['spec']['dnsNames']
    cp=resource(docs,'Deployment','fdp-control-plane')['spec']['template']['spec']['containers'][0]
    env={v['name']:v.get('value') for v in cp['env']}
    assert env['FDP_DIRECTORY_MODE']=='scim'
    assert env['FDP_OIDC_ISSUER']=='https://idp.example.com/realms/fdp'
    assert env['FDP_SECRETS_BACKEND']=='openbao' and env['FDP_OPENBAO_AUTH']=='kubernetes'
    pod=resource(docs,'Deployment','fdp-directory-sync')['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False
    refs={v['valueFrom']['secretKeyRef']['key'] for v in pod['containers'][0]['env'] if 'valueFrom' in v}
    assert refs=={'provisioner-client-secret','FDP_SCIM_TOKEN'}
    idp=resource(docs,'Deployment','fdp-keycloak')['spec']['template']['spec']
    assert idp['automountServiceAccountToken'] is False
    password=next(v for v in idp['containers'][0]['env'] if v['name']=='KC_DB_PASSWORD')
    assert password['valueFrom']['secretKeyRef']['key']=='keycloak-password'
    assert not any(v.get('valueFrom',{}).get('secretKeyRef',{}).get('key') in ('postgres-password','fdp-password')
                   for v in idp['containers'][0]['env'])


def test_external_profile_does_not_install_identity_or_bundled_secret_storage():
    docs=render('-f',str(CHART/'examples/external-services.yaml'))
    names={d['metadata']['name'] for d in docs}
    assert not names & {'fdp-keycloak','fdp-directory-sync','fdp-secrets'}
    assert not any(d['kind']=='Secret' for d in docs)


def test_keycloak_can_use_an_external_database_independently_of_metadata_database():
    docs=render('--set','keycloak.enabled=true','--set','keycloak.database.existingSecret=external-idp-db')
    assert not any(d['kind']=='StatefulSet' and d['metadata']['name']=='fdp-metadata-db' for d in docs)
    pod=resource(docs,'Deployment','fdp-keycloak')['spec']['template']['spec']
    env={v['name']:v for v in pod['containers'][0]['env']}
    assert env['KC_DB_PASSWORD']['valueFrom']['secretKeyRef']=={'name':'external-idp-db','key':'password'}


@pytest.mark.parametrize('args',[
    ['--set','keycloak.enabled=true'],
    ['--set','keycloak.enabled=true','--set','metadataDatabase.enabled=true','--set','oidc.directoryMode=oidc'],
    ['--set','secretManagement.backend=kubernetes','--set','secretManagement.namespace=fdp'],
    ['--set','bootstrap.enabled=true'],
    ['--set','bootstrap.enabled=true','--set','metadataDatabase.enabled=true'],
    ['--set','existingSecret=custom-platform-secret'],
])
def test_invalid_dependency_profiles_fail_before_deployment(args):
    with pytest.raises(subprocess.CalledProcessError):render(*args)


def test_control_plane_and_controller_agree_on_layer_namespaces():
    docs = render('--set', 'layerController.enabled=true')
    def env(name):
        deployment = resource(docs, 'Deployment', name)
        return {e['name']: e.get('value') for e in deployment['spec']['template']['spec']['containers'][0]['env']}
    prefix = env('fdp-layer-controller')['FDP_LAYER_NAMESPACE_PREFIX']
    assert prefix and env('fdp-control-plane')['FDP_LAYER_NAMESPACE_PREFIX'] == prefix
    # The artifact holder reuses the platform image, so no extra image has to be deployed.
    assert env('fdp-layer-controller')['FDP_LAYER_ARTIFACT_IMAGE'] == 'fdp/control-plane:0.1.0'
    assert 'FDP_LAYER_NAMESPACE_PREFIX' not in {
        e['name'] for e in resource(render(), 'Deployment', 'fdp-control-plane')['spec']['template']['spec']['containers'][0]['env']}


def test_install_without_shared_nfs_volume():
    docs = render('--set', 'dataVolume.enabled=false', '--set', 'layerController.enabled=true')
    assert not any(d['kind'] in ('PersistentVolume',) or (d['kind'] == 'PersistentVolumeClaim' and d['metadata']['name'] == 'fdp-data')
                   for d in docs)
    pod = resource(docs, 'Deployment', 'fdp-control-plane')['spec']['template']['spec']
    assert next(v for v in pod['volumes'] if v['name'] == 'data') == {'name': 'data', 'emptyDir': {}}
    env = {e['name']: e.get('value') for e in pod['containers'][0]['env']}
    assert env['FDP_SHARED_DATA_VOLUME'] == 'false'
    default = render()
    assert any(d['kind'] == 'PersistentVolume' for d in default)  # unchanged by default


def test_opt_in_automation_client_is_an_admin_service_account_provisioned_by_sync():
    import json
    def realm(*args):
        docs = render('--set', 'keycloak.enabled=true', '--set', 'metadataDatabase.enabled=true',
                      '--set', 'keycloak.publicUrl=https://idp.example.org', '--set', 'oidc.directoryMode=scim', *args)
        text = resource(docs, 'ConfigMap', 'fdp-keycloak-realm')['data']['fdp-realm.json']
        sync = resource(docs, 'Deployment', 'fdp-directory-sync')['spec']['template']['spec']['containers'][0]['env']
        return json.loads(text), {e['name']: e.get('value') for e in sync}, docs
    off, sync_off, _ = realm()
    assert 'fdp-automation' not in {c['clientId'] for c in off['clients']}
    assert 'KEYCLOAK_SERVICE_CLIENTS' not in sync_off
    on, sync_on, docs = realm('--set', 'keycloak.automationClient.enabled=true')
    client = next(c for c in on['clients'] if c['clientId'] == 'fdp-automation')
    assert client['serviceAccountsEnabled'] and not client['publicClient'] and not client['directAccessGrantsEnabled']
    assert client['secret'] == '${FDP_AUTOMATION_SECRET}'  # resolved from the Secret at import, never in the chart
    user = next(u for u in on['users'] if u.get('serviceAccountClientId') == 'fdp-automation')
    assert user['realmRoles'] == ['fdp-admin']
    assert sync_on['KEYCLOAK_SERVICE_CLIENTS'] == 'fdp-automation'
    secret = resource(docs, 'Secret', 'fdp-keycloak')
    assert 'automation-client-secret' in secret['data']


def test_every_image_must_pin_an_explicit_version():
    import subprocess
    chart = str(Path(__file__).resolve().parents[3] / 'deploy/helm/fdp')
    def render_with(*args):
        return subprocess.run(['helm', 'template', 'fdp', chart, *args], capture_output=True, text=True)
    assert render_with().returncode == 0
    for setting in ('trino.image=trinodb/trino', 'runtime.images.vscode=fdp/workspace-vscode:latest',
                    'keycloak.image=quay.io/keycloak/keycloak'):
        refused = render_with('--set', setting)
        assert refused.returncode != 0 and 'must pin an explicit version' in refused.stderr, setting
    for setting in ('trino.image=trinodb/trino@sha256:' + '0' * 64, 'metadataDatabase.image=registry.local:5000/postgres:16'):
        assert render_with('--set', setting).returncode == 0, setting


def test_shared_system_data_namespace_credentials_networks_and_discovery():
    docs = bundled('--set', 'metadataDatabase.namespace=fdp-system-data',
                   '--set', 'metadataDatabase.airflow.enabled=true', '--set', 'layerController.enabled=true')
    assert resource(docs, 'Namespace', 'fdp-system-data')['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
    for kind, name in [('StatefulSet','fdp-metadata-db'),('PersistentVolumeClaim','fdp-metadata-db'),
                       ('Service','fdp-metadata-db'),('ConfigMap','fdp-metadata-bootstrap')]:
        assert resource(docs,kind,name)['metadata']['namespace']=='fdp-system-data'
    secrets=[d for d in docs if d['kind']=='Secret' and d['metadata']['name']=='fdp-metadata-db']
    assert {s['metadata']['namespace'] for s in secrets}=={'fdp','fdp-system-data'}
    assert secrets[0]['data']==secrets[1]['data']
    assert 'airflow-provisioner-password' in secrets[0]['data']
    cp=resource(docs,'Deployment','fdp-control-plane')['spec']['template']['spec']['containers'][0]
    env={v['name']:v for v in cp['env']}
    assert env['FDP_DATABASE_HOST']['value']=='fdp-metadata-db.fdp-system-data.svc'
    assert env['FDP_AIRFLOW_METADATA_PASSWORD']['valueFrom']['secretKeyRef']['key']=='airflow-provisioner-password'
    assert env['FDP_AIRFLOW_METADATA_USER']['value']=='airflow_provisioner'
    controller=resource(docs,'Deployment','fdp-layer-controller')['spec']['template']['spec']['containers'][0]
    assert next(v['value'] for v in controller['env'] if v['name']=='FDP_LAYER_SHARED_DATABASE_NAMESPACE')=='fdp-system-data'
    kc=resource(docs,'Deployment','fdp-keycloak')['spec']['template']['spec']['containers'][0]
    assert next(v['value'] for v in kc['env'] if v['name']=='KC_DB_URL')=='jdbc:postgresql://fdp-metadata-db.fdp-system-data.svc:5432/keycloak'
    deny=next(d for d in docs if d['kind']=='NetworkPolicy' and d['metadata']['namespace']=='fdp-system-data' and d['metadata']['name']=='default-deny')['spec']
    assert deny['ingress']==deny['egress']==[] and deny['policyTypes']==['Ingress','Egress']
    ingress=resource(docs,'NetworkPolicy','fdp-metadata-db-ingress')['spec']['ingress']
    peer=ingress[1]['from'][0]
    assert 'fdp.floren.app/installation' in peer['namespaceSelector']['matchLabels']
    assert peer['podSelector']['matchExpressions'][0]['values']==['standalone','scheduler','web','worker']
    bootstrap=resource(docs,'ConfigMap','fdp-metadata-bootstrap')['data']['init.sql']
    assert 'LOGIN NOINHERIT CREATEDB CREATEROLE' in bootstrap and 'REVOKE ALL ON DATABASE fdp FROM PUBLIC' in bootstrap


@pytest.mark.parametrize('setting',['metadataDatabase.namespace=fdp-runtime','metadataDatabase.namespace=fdp-secrets',
                                   'metadataDatabase.namespace=Invalid','metadataDatabase.airflow.enabled=true'])
def test_invalid_shared_database_configuration_fails_before_deployment(setting):
    args=['--set',setting]
    if not setting.endswith('enabled=true'):args+=['--set','metadataDatabase.enabled=true']
    with pytest.raises(subprocess.CalledProcessError):render(*args)
