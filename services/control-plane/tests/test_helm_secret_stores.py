"""Secret stores in the chart: the bundled OpenBao, external OpenBao/Vault and cloud secret managers."""
import shutil
import subprocess

import pytest

from .test_helm import CHART, render, resource

pytestmark = pytest.mark.skipif(not shutil.which('helm'), reason='Helm is required for chart rendering checks')


def minimal(*args):
    return render('-f', str(CHART / 'examples/minimal.yaml'), *args)


def env_of(docs):
    container = resource(docs, 'Deployment', 'fdp-control-plane')['spec']['template']['spec']['containers'][0]
    return {e['name']: e.get('value') for e in container['env']}


def openbao_config(docs):
    return resource(docs, 'ConfigMap', 'fdp-openbao-config')['data']


def test_all_in_one_profile_bundles_a_self_initialized_openbao_without_stored_root_or_unseal_keys():
    docs = minimal()
    ns = resource(docs, 'Namespace', 'fdp-openbao')['metadata']
    assert ns['labels']['pod-security.kubernetes.io/enforce'] == 'restricted'
    assert ns['annotations']['helm.sh/resource-policy'] == 'keep'
    # The chart renders no Secret in the OpenBao namespace: the seal key is created once inside the cluster.
    assert not [d for d in docs if d['kind'] == 'Secret' and d['metadata'].get('namespace') == 'fdp-openbao']
    role = resource(docs, 'Role', 'fdp-openbao-seal-key')
    assert role['rules'] == [{'apiGroups': [''], 'resources': ['secrets'], 'resourceNames': ['fdp-openbao-seal'], 'verbs': ['get']},
                             {'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['create']}]
    config = openbao_config(docs)
    assert 'initialize "platform"' in config['initialize.hcl'] and 'initialize' not in config['server.hcl']
    for hcl in config.values():
        assert 'seal "static"' in hcl and 'audit "file" "stdout"' in hcl and 'tls_disable     = true' in hcl
    assert 'bound_service_account_namespaces = ["fdp"]' in config['initialize.hcl']
    assert 'path \\"fdp/data/*\\"' in config['initialize.hcl'] and 'sys/' not in config['initialize.hcl'].split('policy =')[1].split('\n')[0]
    admin = resource(docs, 'ServiceAccount', 'fdp-openbao-admin')
    assert admin['automountServiceAccountToken'] is False
    assert 'bound_service_account_names      = ["fdp-openbao-admin"]' in config['initialize.hcl']
    assert not any(d['kind'] in ('Deployment', 'StatefulSet', 'Job') and
                   d['spec']['template']['spec'].get('serviceAccountName') == 'fdp-openbao-admin' for d in docs)
    env = env_of(docs)
    assert env['FDP_SECRETS_BACKEND'] == 'openbao' and env['FDP_OPENBAO_AUTH'] == 'kubernetes'
    assert env['FDP_OPENBAO_ADDR'] == 'http://fdp-openbao.fdp-openbao.svc:8200' and 'FDP_OPENBAO_TOKEN' not in env


def test_bundled_openbao_pods_are_restricted_and_only_the_control_plane_may_call_them():
    docs = minimal()
    sts = resource(docs, 'StatefulSet', 'fdp-openbao')['spec']
    assert sts['podManagementPolicy'] == 'OrderedReady' and sts['replicas'] == 1
    pod = sts['template']['spec']
    assert pod['securityContext']['runAsNonRoot'] is True and pod['securityContext']['runAsUser'] == 100
    for container in pod['containers'] + pod['initContainers']:
        assert container['securityContext'] == {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                                 'capabilities': {'drop': ['ALL']}}
    seal = next(v for v in pod['volumes'] if v['name'] == 'seal')
    assert seal['emptyDir']['medium'] == 'Memory'
    policy = resource(docs, 'NetworkPolicy', 'fdp-openbao')['spec']
    callers = policy['ingress'][0]['from']
    assert callers == [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'fdp'}},
                        'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'fdp-control-plane'}}}]
    assert resource(docs, 'NetworkPolicy', 'default-deny')['metadata']['namespace'] == 'fdp-openbao'
    probes = pod['containers'][0]
    assert 'sealedcode=204' in probes['livenessProbe']['httpGet']['path']
    assert 'sealedcode' not in probes['readinessProbe']['httpGet']['path']


def test_raft_high_availability_joins_every_member_and_limits_disruption():
    docs = minimal('--set', 'openbao.replicas=3')
    config = openbao_config(docs)['server.hcl']
    assert config.count('retry_join') == 3 and 'fdp-openbao-2.fdp-openbao-internal.fdp-openbao.svc' in config
    assert resource(docs, 'PodDisruptionBudget', 'fdp-openbao')['spec']['maxUnavailable'] == 1
    with pytest.raises(subprocess.CalledProcessError):
        minimal('--set', 'openbao.replicas=2')


def test_kms_seal_stores_no_key_in_the_cluster():
    docs = minimal('--set', 'openbao.seal.mode=kms', '--set', 'openbao.seal.kms.type=awskms',
                   '--set', 'openbao.seal.kms.config.region=eu-west-1', '--set', 'openbao.seal.kms.config.kms_key_id=alias/fdp',
                   '--set', 'openbao.serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn=arn:aws:iam::1:role/bao')
    hcl = openbao_config(docs)['initialize.hcl']
    assert 'seal "awskms"' in hcl and 'kms_key_id = "alias/fdp"' in hcl and 'seal "static"' not in hcl
    pod = resource(docs, 'StatefulSet', 'fdp-openbao')['spec']['template']['spec']
    assert 'initContainers' not in pod and not any(v['name'] == 'seal' for v in pod['volumes'])
    assert not any(d['kind'] == 'Role' and d['metadata']['name'] == 'fdp-openbao-seal-key' for d in docs)
    assert resource(docs, 'ServiceAccount', 'fdp-openbao')['metadata']['annotations'] == {
        'eks.amazonaws.com/role-arn': 'arn:aws:iam::1:role/bao'}
    with pytest.raises(subprocess.CalledProcessError):
        minimal('--set', 'openbao.seal.mode=kms')


def test_an_existing_seal_key_secret_is_mounted_group_readable():
    docs = minimal('--set', 'openbao.seal.existingSecret=my-seal-key')
    pod = resource(docs, 'StatefulSet', 'fdp-openbao')['spec']['template']['spec']
    assert next(v for v in pod['volumes'] if v['name'] == 'seal')['secret'] == {'secretName': 'my-seal-key', 'defaultMode': 0o440}
    assert 'initContainers' not in pod


def test_cert_manager_tls_covers_every_member_and_reloads_renewals():
    docs = minimal('--set', 'openbao.replicas=3', '--set', 'openbao.tls.mode=certManager',
                   '--set', 'openbao.tls.issuerRef.name=internal-ca', '--set', 'openbao.tls.issuerRef.kind=ClusterIssuer')
    cert = resource(docs, 'Certificate', 'fdp-openbao')['spec']
    assert cert['issuerRef']['name'] == 'internal-ca' and cert['ipAddresses'] == ['127.0.0.1']
    assert 'fdp-openbao-2.fdp-openbao-internal.fdp-openbao.svc' in cert['dnsNames']
    pod = resource(docs, 'StatefulSet', 'fdp-openbao')['spec']['template']['spec']
    assert pod['shareProcessNamespace'] is True and [c['name'] for c in pod['containers']] == ['openbao', 'tls-reloader']
    assert pod['containers'][0]['readinessProbe']['httpGet']['scheme'] == 'HTTPS'
    assert 'leader_ca_cert_file' in openbao_config(docs)['server.hcl']
    assert env_of(docs)['FDP_OPENBAO_ADDR'] == 'https://fdp-openbao.fdp-openbao.svc:8200'


@pytest.mark.parametrize('args', [
    ['--set', 'openbao.enabled=true', '--set', 'secretManagement.backend=kubernetes'],
    ['--set', 'secretManagement.backend=azure'],
    ['--set', 'secretManagement.backend=gcp'],
    ['--set', 'secretManagement.backend=aws', '--set', 'secretManagement.aws.recoveryWindowDays=3'],
    ['--set', 'secretManagement.backend=hashicorp'],
    ['--set', 'openbao.enabled=true', '--set', 'openbao.namespace=fdp'],
    ['--set', 'openbao.enabled=true', '--set', 'openbao.image=ghcr.io/openbao/openbao:latest'],
])
def test_invalid_secret_store_settings_fail_before_deployment(args):
    with pytest.raises(subprocess.CalledProcessError):
        render(*args)


def test_bootstrap_works_with_secret_stores_that_need_no_stored_token():
    base = ['-f', str(CHART / 'examples/bundled.yaml')]
    render(*base)   # bundled OpenBao
    render(*base, '--set', 'openbao.enabled=false', '--set', 'openbao.auth.method=kubernetes')   # external, Kubernetes auth
    with pytest.raises(subprocess.CalledProcessError):
        render(*base, '--set', 'openbao.enabled=false')   # external with a token: the platform Secret must hold it


def test_external_vault_with_kubernetes_auth_and_enterprise_namespace():
    env = env_of(render('--set', 'secretManagement.backend=vault', '--set', 'openbao.addr=https://vault.example.com:8200',
                        '--set', 'openbao.auth.method=kubernetes', '--set', 'openbao.auth.mount=k8s-fdp',
                        '--set', 'openbao.enterpriseNamespace=data-platform'))
    assert env['FDP_SECRETS_BACKEND'] == 'vault' and env['FDP_OPENBAO_ADDR'] == 'https://vault.example.com:8200'
    assert env['FDP_OPENBAO_AUTH'] == 'kubernetes' and env['FDP_OPENBAO_AUTH_MOUNT'] == 'k8s-fdp'
    assert env['FDP_OPENBAO_NAMESPACE'] == 'data-platform'


@pytest.mark.parametrize('args,expected', [
    (['--set', 'secretManagement.backend=aws', '--set', 'secretManagement.aws.region=eu-west-1',
      '--set', 'secretManagement.aws.kmsKeyId=alias/fdp'],
     {'FDP_AWS_SECRETS_REGION': 'eu-west-1', 'FDP_AWS_SECRETS_KMS_KEY_ID': 'alias/fdp', 'FDP_AWS_SECRETS_RECOVERY_WINDOW_DAYS': '7'}),
    (['--set', 'secretManagement.backend=azure', '--set', 'secretManagement.azure.vaultUrl=https://fdp.vault.azure.net/'],
     {'FDP_AZURE_KEY_VAULT_URL': 'https://fdp.vault.azure.net/', 'FDP_AZURE_SECRETS_PREFIX': 'fdp-'}),
    (['--set', 'secretManagement.backend=gcp', '--set', 'secretManagement.gcp.project=fdp-prod',
      '--set', 'secretManagement.gcp.locations={europe-west4,europe-west1}'],
     {'FDP_GCP_SECRETS_PROJECT': 'fdp-prod', 'FDP_GCP_SECRETS_LOCATIONS': 'europe-west4,europe-west1'}),
])
def test_cloud_secret_managers_use_the_control_plane_workload_identity(args, expected):
    docs = render(*args, '--set', 'serviceAccount.annotations.iam\\.gke\\.io/gcp-service-account=fdp@p.iam.gserviceaccount.com',
                  '--set-string', 'podLabels.azure\\.workload\\.identity/use=true')
    env = env_of(docs)
    assert expected.items() <= env.items() and not any(k.startswith('FDP_OPENBAO') for k in env)
    assert resource(docs, 'ServiceAccount', 'fdp-control-plane')['metadata']['annotations'] == {
        'iam.gke.io/gcp-service-account': 'fdp@p.iam.gserviceaccount.com'}
    labels = resource(docs, 'Deployment', 'fdp-control-plane')['spec']['template']['metadata']['labels']
    assert labels['azure.workload.identity/use'] == 'true' and labels['app.kubernetes.io/name'] == 'fdp-control-plane'
    assert not any(d['metadata'].get('namespace') == 'fdp-openbao' for d in docs)


def test_existing_kubernetes_secret_installations_render_no_openbao():
    docs = render('-f', str(CHART / 'examples/bundled.yaml'), '--set', 'openbao.enabled=false',
                  '--set', 'secretManagement.backend=kubernetes')
    assert not any('openbao' in d['metadata']['name'] for d in docs)
    assert env_of(docs)['FDP_SECRETS_BACKEND'] == 'kubernetes'


def test_cilium_clusters_get_an_explicit_kube_apiserver_rule():
    assert not any(d['kind'] == 'CiliumNetworkPolicy' for d in minimal())
    docs = minimal('--api-versions', 'cilium.io/v2/CiliumNetworkPolicy')
    rule = resource(docs, 'CiliumNetworkPolicy', 'fdp-openbao-kube-apiserver')['spec']
    assert rule['endpointSelector'] == {'matchLabels': {'app.kubernetes.io/name': 'fdp-openbao'}}
    assert rule['egress'][0]['toEntities'] == ['kube-apiserver']
    assert not any(d['kind'] == 'CiliumNetworkPolicy' for d in
                   minimal('--api-versions', 'cilium.io/v2/CiliumNetworkPolicy', '--set', 'openbao.networkPolicy.cilium=false'))
