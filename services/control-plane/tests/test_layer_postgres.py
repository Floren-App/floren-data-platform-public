from copy import deepcopy

import pytest

from app import layer_controller as controller, layer_infrastructure as infra, layers
from app.layer_manifests import manifests, ProvisioningError
from .conftest import as_
from .test_layer_controller import WORKERS, CONFIG, MemoryKubernetes, due, operation, queue


def postgres_spec(mode='existing'):
    tls = {'mode': mode, 'secret_name': 'postgres-tls'} if mode != 'disabled' else {}
    if mode == 'cert_manager':
        tls['issuer'] = {'name': 'layer-ca'}
    return infra.InfrastructureSpec(enabled=True, trino={'workers': {'warehouse': 'trino'}}, postgres={'enabled': True,
        'credentials_secret': 'postgres-passwords', 'tls': tls})


def test_database_credentials_storage_and_tls_stay_in_its_layer():
    docs = manifests(layers.NON_PROD, 1, postgres_spec('cert_manager'), CONFIG, workers=WORKERS)
    postgres = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'fdp-postgres')
    pod = postgres['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False
    assert pod['securityContext']['runAsUser'] == 999
    assert len(pod['containers']) == 2
    auth = next(v['secret'] for v in pod['volumes'] if v['name'] == 'auth')
    assert auth['secretName'] == 'postgres-passwords'
    assert {v['key'] for v in auth['items']} == {'postgres-password', 'engine-password'}
    assert 'auth' not in {m['name'] for m in pod['containers'][1]['volumeMounts']}
    tls = next(v['secret'] for v in pod['volumes'] if v['name'] == 'tls-input')
    assert tls['defaultMode'] == 0o640
    assert not any(d['kind'] == 'Secret' for d in docs)
    trino = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'fdp-trino-coordinator')
    assert 'postgres-passwords' not in str(trino)
    pvc = next(d for d in docs if d['kind'] == 'PersistentVolumeClaim' and d['metadata']['name'] == 'fdp-postgres')
    assert pvc['metadata']['annotations']['fdp.floren.app/retention'] == 'Retain'
    assert pvc['spec']['accessModes'] == ['ReadWriteOnce']
    certificate = next(d for d in docs if d['kind'] == 'Certificate')
    assert certificate['spec']['dnsNames'] == ['fdp-postgres.fdp-layer-%s.svc' % layers.NON_PROD]
    config = next(d['data'] for d in docs if d['kind'] == 'ConfigMap' and d['metadata']['name'] == 'fdp-postgres')
    assert 'hostnossl all all 0.0.0.0/0 reject' in config['pg_hba.conf']
    assert 'NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION' in config['10-engine.sh']
    ingress = next(d['spec'] for d in docs if d['metadata']['name'] == 'postgres-ingress')
    assert ingress['ingress'][0]['from'][0] == {'podSelector': {'matchLabels': {
        'fdp.floren.app/layer': layers.NON_PROD, 'app.kubernetes.io/component': 'trino'}}}
    assert all('namespaceSelector' in peer for peer in ingress['ingress'][0]['from'][1:])


@pytest.mark.parametrize('quota', [{'cpu': 1.5}, {'memory': '4Gi'}, {'storage': '50Gi'},
                                  {'persistent_volume_claims': 1}])
def test_database_and_reload_resources_must_fit_the_layer_budget(quota):
    spec = postgres_spec().model_dump()
    spec['quota'].update(quota)
    with pytest.raises(ProvisioningError) as exc:
        manifests(layers.NON_PROD, 1, infra.InfrastructureSpec.model_validate(spec), CONFIG, workers=WORKERS)
    assert exc.value.reason == 'InsufficientQuota'


def test_verified_database_tls_gates_layer_readiness(client):
    operation_id = queue(client, postgres_spec().model_dump())
    api = MemoryKubernetes()
    resources = controller.KubernetesResources(api)
    resources.verify_postgres = lambda hostname: False
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    api.rollouts_ready()
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).reason == 'WaitingForVerifiedPostgresTLS'
    resources.verify_postgres = lambda hostname: True
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).phase == 'Ready'


def test_every_certificate_must_be_ready_not_only_the_last_one(client):
    spec = postgres_spec('cert_manager').model_dump()
    spec['trino']['tls'] = {'mode': 'cert_manager', 'secret_name': 'trino-tls', 'issuer': {'name': 'layer-ca'}}
    operation_id = queue(client, spec)
    api = MemoryKubernetes()
    resources = controller.KubernetesResources(api)
    resources.verify_https = resources.verify_postgres = lambda hostname: True
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    api.rollouts_ready()
    for doc in api.objects.values():
        if doc['kind'] == 'Certificate' and doc['metadata']['name'] == 'fdp-trino':
            doc['status'] = {'conditions': [{'type': 'Ready', 'status': 'True', 'observedGeneration': doc['metadata']['generation']}]}
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).reason == 'WaitingForCertificate'
    for doc in api.objects.values():
        if doc['kind'] == 'Certificate':
            doc['status'] = {'conditions': [{'type': 'Ready', 'status': 'True', 'observedGeneration': doc['metadata']['generation']}]}
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).phase == 'Ready'


def test_database_updates_cannot_drop_storage_change_bootstrap_credentials_or_disable_tls(client):
    spec = postgres_spec().model_dump()
    operation_id = queue(client, spec)
    due(operation_id, phase='Ready')
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    for changes in ({'enabled': False}, {'size': '1Gi'}, {'image': 'postgres:17'},
                    {'credentials_secret': 'other-passwords'}, {'tls': {'mode': 'disabled'}}):
        desired = deepcopy(spec)
        desired['postgres'].update(changes)
        response = client.put(path, headers=as_('admin'), json={'expected_generation': 1, 'spec': desired})
        assert response.status_code == 409, response.text
