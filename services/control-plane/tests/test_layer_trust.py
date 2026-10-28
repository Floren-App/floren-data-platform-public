"""Layer executions receive the platform's public CA bundle, and nothing else, from their own layer."""
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from app import layers
from app.config import get_settings
from app.db import SessionLocal
from app.layer_infrastructure import InfrastructureSpec, placement
from app.layer_manifests import ProvisioningError, manifests
from app.orchestrator.base import Placement, ExecutionSpec
from .conftest import as_
from .test_layer_controller import WORKERS, CONFIG
from .test_layer_infrastructure import managed
from .test_layer_workspaces import orchestrator
from .test_helm import render, resource


def ca_pem(name='Private Root'):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key


def config_with(tmp_path, content):
    from dataclasses import replace
    path = tmp_path / 'ca-bundle.pem'
    path.write_text(content)
    return replace(CONFIG, trust_file=str(path))


def docs(config):
    return manifests(layers.NON_PROD, 1, InfrastructureSpec.model_validate(managed()), config, workers=WORKERS)


def test_controller_publishes_only_public_ca_certificates_to_the_layer(tmp_path):
    first, _ = ca_pem('Root A')
    second, _ = ca_pem('Root B')
    trust = next(d for d in docs(config_with(tmp_path, first + second + first)) if d['metadata']['name'] == 'fdp-trust')
    assert trust['kind'] == 'ConfigMap' and trust['metadata']['namespace'].endswith(layers.NON_PROD)
    bundle = trust['data']['ca-bundle.pem']
    assert bundle.count('BEGIN CERTIFICATE') == 2 and 'PRIVATE KEY' not in bundle  # deduplicated
    assert not any(d['metadata']['name'] == 'fdp-trust' for d in docs(CONFIG))  # not configured: nothing


def test_bundles_with_private_keys_or_leaf_certificates_are_never_distributed(tmp_path):
    pem, key = ca_pem()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    for content in (pem + private, '', 'not a certificate'):
        with pytest.raises(ProvisioningError) as error:
            docs(config_with(tmp_path, content))
        assert error.value.reason == 'InvalidTrustBundle' and 'BEGIN' not in error.value.message


def test_layer_workloads_mount_the_bundle_and_point_common_clients_at_it():
    orch = orchestrator()
    namespace = 'dc-x-layer-' + layers.NON_PROD
    orch.start(ExecutionSpec(name='run-1', image='python:1', cpu=1, memory_gb=1, env={'SSL_CERT_FILE': 'ignored'},
                            placement=Placement(namespace, trust_configmap='fdp-trust')))
    pod = orch.core.objects[(namespace, 'pod', 'run-1')]
    volume = next(v for v in pod.spec.volumes if v.name == 'trust')
    assert volume.config_map.name == 'fdp-trust'
    container = pod.spec.containers[0]
    assert any(m.name == 'trust' and m.read_only and m.mount_path == '/etc/fdp/trust' for m in container.volume_mounts)
    env = {e.name: e.value for e in container.env}
    for name in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'AWS_CA_BUNDLE', 'GIT_SSL_CAINFO', 'NODE_EXTRA_CA_CERTS'):
        assert env[name] == '/etc/fdp/trust/ca-bundle.pem'
    orch.start(ExecutionSpec(name='run-2', image='python:1', cpu=1, memory_gb=1, placement=Placement(namespace)))
    assert not any(v.name == 'trust' for v in orch.core.objects[(namespace, 'pod', 'run-2')].spec.volumes)


def test_placement_requests_trust_only_when_the_platform_has_a_bundle(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'layer_namespace_prefix', 'dc-x-layer')
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    assert client.put(path, headers=as_('admin'), json={'expected_generation': 0, 'spec': managed()}).status_code == 202
    with SessionLocal() as db:
        assert placement(db, layers.NON_PROD).trust_configmap is None
        monkeypatch.setattr(get_settings(), 'layer_trust_bundle', True)
        assert placement(db, layers.NON_PROD).trust_configmap == 'fdp-trust'


def test_chart_wires_the_bundle_to_controller_and_control_plane_only_when_configured():
    def env(rendered, name):
        container = resource(rendered, 'Deployment', name)['spec']['template']['spec']['containers'][0]
        return {e['name']: e.get('value') for e in container['env']}
    on = render('--set', 'layerController.enabled=true', '--set', 'trust.caBundleSecret=platform-ca')
    assert env(on, 'fdp-layer-controller')['FDP_LAYER_TRUST_FILE'] == '/etc/fdp/trust/ca-bundle.pem'
    assert env(on, 'fdp-control-plane')['FDP_LAYER_TRUST_BUNDLE'] == 'true'
    off = render('--set', 'layerController.enabled=true')
    assert 'FDP_LAYER_TRUST_FILE' not in env(off, 'fdp-layer-controller')
    assert 'FDP_LAYER_TRUST_BUNDLE' not in env(off, 'fdp-control-plane')


def test_shared_namespace_workloads_mount_the_platform_bundle_when_configured():
    orch = orchestrator()
    orch.start(ExecutionSpec(name='run-3', image='python:1', cpu=1, memory_gb=1))
    assert not any(v.name == 'trust' for v in orch.core.objects[(orch.ns, 'pod', 'run-3')].spec.volumes)
    orch.trust_configmap = 'fdp-trust'
    orch.start(ExecutionSpec(name='run-4', image='python:1', cpu=1, memory_gb=1))
    pod = orch.core.objects[(orch.ns, 'pod', 'run-4')]
    assert next(v for v in pod.spec.volumes if v.name == 'trust').config_map.name == 'fdp-trust'
    env = {e.name: e.value for e in pod.spec.containers[0].env}
    assert env['SSL_CERT_FILE'] == '/etc/fdp/trust/ca-bundle.pem'


def test_chart_copies_only_the_public_bundle_into_the_shared_workload_namespace():
    on = render('--set', 'trust.caBundleSecret=platform-ca')
    trust = resource(on, 'ConfigMap', 'fdp-trust')
    assert trust['metadata']['namespace'] == 'fdp-runtime' and list(trust['data']) == ['ca-bundle.pem']
    container = resource(on, 'Deployment', 'fdp-control-plane')['spec']['template']['spec']['containers'][0]
    assert {'name': 'FDP_K8S_TRUST_CONFIGMAP', 'value': 'fdp-trust'} in container['env']
    assert not any(d for d in render() if d and d['kind'] == 'ConfigMap' and d['metadata']['name'] == 'fdp-trust')
