import asyncio
import ssl

import pytest

from app import layer_controller as controller, layer_infrastructure as infra, layers, sql_access, tls_bundle
from app.db import SessionLocal
from app.layer_manifests import manifests
from app.models import Resource, SqlQuery, utcnow
from datetime import timedelta
from .certificate_helpers import certificate_pair, write_pair
from .conftest import as_
from .test_layer_controller import WORKERS, CONFIG, MemoryKubernetes, due, operation, queue
from .test_layer_sql import layered, ready  # noqa: F401
from .test_shared_sql import sql_mode  # noqa: F401

HOSTNAME = 'fdp-trino.fdp-layer-%s.svc' % layers.NON_PROD


def tls_spec(mode='existing'):
    tls = {'mode': mode, 'secret_name': 'trino-tls'}
    if mode == 'cert_manager':
        tls['issuer'] = {'name': 'internal-ca', 'kind': 'ClusterIssuer'}
    return infra.InfrastructureSpec(enabled=True, trino={'tls': tls, 'workers': {'warehouse': 'trino'}})


def test_certificates_stay_local_and_only_https_is_exposed_to_control_plane():
    docs = manifests(layers.NON_PROD, 1, tls_spec('cert_manager'), CONFIG, workers=WORKERS)
    certificate = next(doc for doc in docs if doc['kind'] == 'Certificate')
    assert certificate['metadata']['namespace'] == 'fdp-layer-' + layers.NON_PROD
    assert certificate['spec']['dnsNames'] == [HOSTNAME]
    assert certificate['spec']['privateKey']['rotationPolicy'] == 'Always'
    assert certificate['spec']['issuerRef']['kind'] == 'ClusterIssuer'
    gateway = next(doc for doc in docs if doc['metadata']['name'] == 'trino-gateway')
    assert gateway['spec']['ingress'][0]['ports'] == [{'port': 8443, 'protocol': 'TCP'}]
    for doc in docs:
        if doc['kind'] != 'Deployment' or 'trino-' not in doc['metadata']['name']:
            continue
        pod = doc['spec']['template']['spec']
        assert pod['automountServiceAccountToken'] is False
        assert pod['initContainers'][0]['command'][-1] == '--once'
        assert [container['name'] for container in pod['containers']] == ['trino', 'tls-bundle']
        assert next(volume for volume in pod['volumes'] if volume['name'] == 'tls-runtime')['emptyDir'] == {'medium': 'Memory'}
        assert next(volume for volume in pod['volumes'] if volume['name'] == 'tls-input')['secret']['secretName'] == 'trino-tls'
        pod['initContainers'][0]['volumeMounts'].append({'name': 'init-only', 'mountPath': '/init-only'})
        assert not any(mount['name'] == 'init-only' for mount in pod['containers'][1]['volumeMounts'])
    config = next(doc for doc in docs if doc['kind'] == 'ConfigMap')['data']['config.properties']
    assert 'http-server.authentication.allow-insecure-over-http=false' in config
    assert 'http-server.https.enabled=true' in config
    assert 'http-server.https.ssl-context.refresh-time=10s' in config


def test_controller_waits_for_certificate_generation(client):
    operation_id = queue(client, tls_spec('cert_manager').model_dump())
    api = MemoryKubernetes()
    resources = controller.KubernetesResources(api)
    resources.verify_https = lambda hostname: True
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).reason == 'WaitingForCertificate'
    api.rollouts_ready()
    certificate = next(doc for doc in api.objects.values() if doc['kind'] == 'Certificate')
    certificate['status'] = {'conditions': [{'type': 'Ready', 'status': 'True', 'observedGeneration': 0}]}
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).reason == 'WaitingForCertificate'
    certificate = next(doc for doc in api.objects.values() if doc['kind'] == 'Certificate')
    certificate['status']['conditions'][0]['observedGeneration'] = certificate['metadata']['generation']
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).phase == 'Ready'


def test_tls_readiness_failure_blocks_admission_until_verified(client):
    operation_id = queue(client, tls_spec().model_dump())
    api = MemoryKubernetes()
    resources = controller.KubernetesResources(api)
    resources.verify_https = lambda hostname: False
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    api.rollouts_ready()
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).reason == 'WaitingForVerifiedTLS'
    resources.verify_https = lambda hostname: True
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).phase == 'Ready'


def test_https_upgrade_waits_for_queries_and_cannot_be_downgraded(client):
    operation_id = queue(client)
    due(operation_id, phase='Ready')
    with SessionLocal() as db:
        db.add(SqlQuery(id='tls-query', principal_id='principal', actor='admin', binding='user:admin',
            engine_layer=layers.NON_PROD, state='RUNNING', expires_at=utcnow()+timedelta(minutes=10)))
        db.commit()
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    response = client.put(path, headers=as_('admin'), json={'expected_generation': 1, 'spec': tls_spec().model_dump()})
    assert response.status_code == 409 and 'active layer SQL' in response.text
    with SessionLocal() as db:
        db.get(SqlQuery, 'tls-query').state = 'FINISHED'
        db.commit()
    response = client.put(path, headers=as_('admin'), json={'expected_generation': 1, 'spec': tls_spec().model_dump()})
    assert response.status_code == 202
    due(response.json()['operation']['id'], phase='Ready')
    plaintext = infra.InfrastructureSpec(enabled=True, trino={'workers': {'warehouse': 'trino'}}).model_dump()
    response = client.put(path, headers=as_('admin'), json={'expected_generation': 2, 'spec': plaintext})
    assert response.status_code == 409 and 'plaintext' in response.text


def test_bundle_rotation_checks_key_identity_expiry_and_permissions(tmp_path):
    incoming, runtime = tmp_path/'incoming', tmp_path/'runtime'
    runtime.mkdir()
    key, cert, ca = certificate_pair(HOSTNAME)
    write_pair(incoming, key, cert)
    output = runtime/'keystore.pem'
    assert tls_bundle.publish(incoming, output, HOSTNAME)
    first = output.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    key2, cert2, _ = certificate_pair(HOSTNAME, ca=ca)
    write_pair(incoming, key2, cert)  # mismatched pair must not replace a working PEM
    assert not tls_bundle.publish(incoming, output, HOSTNAME)
    assert output.read_bytes() == first and not (runtime/'ready').exists()
    write_pair(incoming, key2, cert2)
    assert tls_bundle.publish(incoming, output, HOSTNAME)
    assert output.read_bytes() != first and (runtime/'ready').exists()
    key3, expired, _ = certificate_pair(HOSTNAME, ca=ca, expired=True)
    write_pair(incoming, key3, expired)
    assert not tls_bundle.publish(incoming, output, HOSTNAME)
    write_pair(incoming, key, cert)
    assert not tls_bundle.publish(incoming, output, 'another-layer.svc')


def test_https_route_is_snapshotted_at_sql_admission(layered):  # noqa: F811
    client, _, sent = layered
    with SessionLocal() as db:
        layer = db.get(Resource, layers.NON_PROD)
        layer.config = {**layer.config, 'infrastructure': tls_spec().model_dump()}
        db.commit()
    ready()
    response = client.post('/v1/sql/query', headers=as_('ana'), json={'source': 'finance', 'sql': 'SELECT 1'})
    assert response.status_code == 200, response.text
    assert sent[0]['url'].startswith('https://fdp-trino.dc-test-layer-%s.svc:8443/' % layers.NON_PROD)


def test_ca_replacement_uses_fresh_verified_transport_and_drains_old_requests(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    bundle = tmp_path/'ca.pem'
    _, _, ca1 = certificate_pair('localhost')
    _, _, ca2 = certificate_pair('localhost')
    bundle.write_bytes(ca1[1].public_bytes(serialization.Encoding.PEM))
    monkeypatch.setenv('SSL_CERT_FILE', str(bundle))
    created = []

    class Client:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.is_closed = False
            created.append(self)

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(sql_access.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(sql_access, '_engine_transport', None)

    async def run():
        async with sql_access.engine_client() as first:
            assert first.options['verify'].verify_mode == ssl.CERT_REQUIRED
            assert first.options['verify'].check_hostname
            bundle.write_bytes(ca2[1].public_bytes(serialization.Encoding.PEM))
            async with sql_access.engine_client() as second:
                assert second is not first and not first.is_closed
                assert second.options['follow_redirects'] is False
            assert not first.is_closed
        assert first.is_closed
        bundle.write_bytes(b'invalid bundle')
        with pytest.raises(Exception) as error:
            async with sql_access.engine_client():
                pass
        assert getattr(error.value, 'status_code', 0) == 503
        await sql_access.close_engine_client()
        assert all(client.is_closed for client in created)

    asyncio.run(run())
