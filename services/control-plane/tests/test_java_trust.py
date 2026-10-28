from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12

from app import java_trust, layers
from app.layer_infrastructure import InfrastructureSpec
from app.layer_manifests import manifests
from .certificate_helpers import certificate_pair
from .test_layer_controller import WORKERS, CONFIG


def test_java_truststore_contains_only_unique_public_ca_certificates(tmp_path):
    key, leaf, ca = certificate_pair('source.test')
    root = ca[1].public_bytes(serialization.Encoding.PEM)
    source, output = tmp_path/'ca.pem', tmp_path/'trust.p12'
    source.write_bytes(root + root)
    java_trust.build(source, output)
    loaded_key, loaded_leaf, loaded_cas = pkcs12.load_key_and_certificates(output.read_bytes(), java_trust.PASSWORD)
    assert loaded_key is loaded_leaf is None
    assert len(loaded_cas) == 1 and loaded_cas[0] == ca[1]
    assert output.stat().st_mode & 0o777 == 0o600
    previous = output.read_bytes()
    for invalid in (b'', key + root, leaf, b'not PEM', root + b'extra data', b'a'*(java_trust.MAX_BYTES+1)):
        source.write_bytes(invalid)
        with pytest.raises(ValueError):
            java_trust.build(source, output)
        assert output.read_bytes() == previous
        assert not list(tmp_path.glob('.trust-*'))


def test_ca_validity_and_signing_constraints_are_enforced():
    _, _, ca = certificate_pair('source.test')
    original = ca[1]
    now = datetime.now(timezone.utc)
    for expired, signing in ((True, True), (False, False)):
        cert = (x509.CertificateBuilder().subject_name(original.subject).issuer_name(original.issuer)
            .public_key(ca[0].public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(days=2)).not_valid_after(now+timedelta(days=-1 if expired else 1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, signing, True, False, False), critical=True)
            .sign(ca[0], hashes.SHA256()))
        with pytest.raises(ValueError):
            java_trust.trusted_certificates(cert.public_bytes(serialization.Encoding.PEM))


def test_kubelet_projection_is_read_and_rotation_uses_the_selected_revision(tmp_path):
    _, _, ca = certificate_pair('source.test')
    revision = tmp_path/'revision'
    revision.mkdir()
    (revision/'ca.pem').write_bytes(ca[1].public_bytes(serialization.Encoding.PEM))
    (tmp_path/'..data').symlink_to(revision, target_is_directory=True)
    # Direct key deliberately differs: the writer must pin ..data instead.
    (tmp_path/'ca.pem').write_bytes(b'invalid')
    java_trust.build(tmp_path/'ca.pem', tmp_path/'trust.p12')
    assert pkcs12.load_key_and_certificates((tmp_path/'trust.p12').read_bytes(), java_trust.PASSWORD)[2] == [ca[1]]


def test_trust_is_built_in_tokenless_layer_local_init_and_used_by_each_java_node():
    spec = InfrastructureSpec(enabled=True, trino={'workers': 1, 'tls': {'mode': 'existing',
        'secret_name': 'server-tls', 'trust_bundle': {'name': 'layer-ca-v2', 'key': 'roots.pem'}}})
    docs = manifests(layers.NON_PROD, 2, spec, CONFIG, workers=WORKERS)
    for doc in docs:
        if doc['kind'] == 'ConfigMap':
            assert '-Djavax.net.ssl.trustStore=/java-trust/truststore.p12' in doc['data']['jvm.config']
            assert '-Djavax.net.ssl.trustStoreType=PKCS12' in doc['data']['jvm.config']
        if doc['kind'] != 'Deployment' or 'trino-' not in doc['metadata']['name']:
            continue
        pod = doc['spec']['template']['spec']
        assert pod['automountServiceAccountToken'] is False
        assert [c['name'] for c in pod['initContainers']] == ['java-trust-init', 'tls-init']
        assert next(v for v in pod['volumes'] if v['name'] == 'java-ca')['secret'] == {
            'secretName': 'layer-ca-v2', 'items': [{'key': 'roots.pem', 'path': 'ca.pem'}]}
        trino = pod['containers'][0]
        assert any(m['name'] == 'java-trust' and m['readOnly'] for m in trino['volumeMounts'])
        assert not any(m['name'] == 'java-ca' for m in trino['volumeMounts'])
        assert all('namespace' not in v.get('secret', {}) for v in pod['volumes'])
