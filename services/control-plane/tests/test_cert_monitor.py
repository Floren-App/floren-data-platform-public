"""Certificate expiry/renewal states, their sources, and the surfaces operators alert on."""
from datetime import datetime, timedelta, timezone
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app import cert_monitor, layers
from app.config import get_settings
from app.db import SessionLocal
from app.models import CertificateCheck
from .conftest import as_

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def pki(tmp_path, hostname='localhost', days=30):
    now = datetime.now(timezone.utc)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Monitor Root')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=365)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), False).sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (tmp_path / 'ca.pem').write_bytes(ca.public_bytes(pem))
    (tmp_path / 'leaf.pem').write_bytes(leaf.public_bytes(pem))
    (tmp_path / 'leaf.key').write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return leaf


def trust(monkeypatch, tmp_path):
    """In the containers SSL_CERT_FILE carries the platform bundle; macOS system Python ignores it."""
    original = ssl.create_default_context
    monkeypatch.setattr(ssl, 'create_default_context', lambda *a, **k: original(cafile=str(tmp_path / 'ca.pem')))


def tls_server(tmp_path, postgres=False):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / 'leaf.pem', tmp_path / 'leaf.key')
    listener = socket.create_server(('127.0.0.1', 0))

    def serve():
        for _ in range(2):
            connection, _ = listener.accept()
            try:
                if postgres:
                    connection.recv(8)
                    connection.sendall(b'S')
                with context.wrap_socket(connection, server_side=True) as secured:
                    secured.recv(1)
            except (OSError, ssl.SSLError):
                pass
    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1]


@pytest.mark.parametrize('args,state', [
    ({'not_after': None}, 'unverified'),
    ({'not_after': NOW - timedelta(minutes=1)}, 'expired'),
    ({'not_after': NOW + timedelta(days=20), 'failure': 'issuer not ready'}, 'renewal_failed'),
    ({'not_after': NOW + timedelta(days=20), 'renewal_time': NOW - timedelta(hours=2)}, 'renewal_overdue'),
    ({'not_after': NOW + timedelta(days=20), 'renewal_time': NOW - timedelta(minutes=10), 'ready': False}, 'renewing'),
    ({'not_after': NOW + timedelta(days=5)}, 'expiring'),
    ({'not_after': NOW + timedelta(days=5), 'renewal_time': NOW + timedelta(days=1)}, 'ok'),  # managed: renews itself
    ({'not_after': NOW + timedelta(days=60)}, 'ok'),
])
def test_states(args, state):
    assert cert_monitor.classify(NOW, **args)[0] == state


def test_cert_manager_status_is_read_from_its_conditions():
    healthy = cert_monitor.manager_status({'notAfter': '2026-12-01T00:00:00Z', 'renewalTime': '2026-11-16T00:00:00Z',
        'conditions': [{'type': 'Ready', 'status': 'True', 'reason': 'Ready'}]})
    assert healthy == {'ready': True, 'failure': None, 'renewal_time': datetime(2026, 11, 16, tzinfo=timezone.utc),
                       'not_after': datetime(2026, 12, 1, tzinfo=timezone.utc)}
    failed = cert_monitor.manager_status({'lastFailureTime': '2026-10-22T11:00:00Z', 'failedIssuanceAttempts': 2,
        'conditions': [{'type': 'Ready', 'status': 'True'},
                       {'type': 'Issuing', 'status': 'False', 'reason': 'Failed', 'message': 'issuer layer-ca is not ready'}]})
    assert failed['failure'] == 'issuer layer-ca is not ready' and failed['ready'] is True
    # A renewal stuck behind an issuer that is not ready has no failure condition: time it out.
    pending = {'conditions': [{'type': 'Ready', 'status': 'True'},
                              {'type': 'Issuing', 'status': 'True', 'lastTransitionTime': '2026-10-22T11:00:00Z'}]}
    assert cert_monitor.manager_status(pending, now=NOW - timedelta(minutes=50))['failure'] is None
    assert 'pending since 2026-10-22T11:00:00' in cert_monitor.manager_status(pending, now=NOW)['failure']


def test_records_the_served_certificate_and_tracks_state_changes(client, tmp_path):
    leaf = pki(tmp_path, days=60)
    with SessionLocal() as db:
        row = cert_monitor.record(db, 'gateway:public', 'gateway', 'fdp.test', served=leaf, now=NOW)
        db.commit()
        assert (row.state, row.serial, row.changed_at) == ('ok', format(leaf.serial_number, 'x'), NOW)
        later = NOW + timedelta(hours=1)
        row = cert_monitor.record(db, 'gateway:public', 'gateway', 'fdp.test', served=leaf, now=later)
        assert row.changed_at == NOW and row.checked_at == later  # unchanged state keeps its start time
        row = cert_monitor.record(db, 'gateway:public', 'gateway', 'fdp.test', served=None, now=later)
        assert row.state == 'unverified' and row.changed_at == later and row.serial is None
        db.commit()


@pytest.mark.parametrize('postgres', [False, True])
def test_served_certificate_needs_a_trusted_verified_handshake(tmp_path, monkeypatch, postgres):
    leaf = pki(tmp_path)
    port = tls_server(tmp_path, postgres=postgres)
    trust(monkeypatch, tmp_path)
    served = cert_monitor.served_certificate('127.0.0.1', port, server_name='localhost', postgres=postgres)
    assert served is not None and served.serial_number == leaf.serial_number
    # Wrong name: the handshake fails, so nothing is reported as served.
    assert cert_monitor.served_certificate('127.0.0.1', port, server_name='other.test', postgres=postgres) is None


def test_gateway_checks_record_each_configured_endpoint(client, tmp_path, monkeypatch):
    pki(tmp_path, days=3)
    port = tls_server(tmp_path)
    trust(monkeypatch, tmp_path)
    assert cert_monitor.gateway_endpoints(f'public=127.0.0.1:{port}/localhost') == [('public', '127.0.0.1', port, 'localhost')]
    cert_monitor.check_gateways(SessionLocal, f'public=127.0.0.1:{port}/localhost')
    with SessionLocal() as db:
        row = db.get(CertificateCheck, 'gateway:public')
        assert row.state == 'expiring' and row.hostname == 'localhost'  # no automatic renewal, 3 days left


def test_admin_api_and_metrics_endpoint(client, tmp_path, monkeypatch):
    leaf = pki(tmp_path, days=60)
    with SessionLocal() as db:
        cert_monitor.record(db, 'gateway:public', 'gateway', 'fdp.test', served=leaf)
        cert_monitor.record(db, f'layer:{layers.NON_PROD}:trino', 'layer', 'fdp-trino.x.svc', layer_id=layers.NON_PROD,
                            served=leaf, status={'lastFailureTime': '2026-10-22T11:00:00Z', 'conditions': []})
        db.commit()
    assert client.get('/v1/admin/certificates', headers=as_('ana')).status_code == 403
    listed = client.get('/v1/admin/certificates', headers=as_('admin')).json()
    assert [c['state'] for c in listed] == ['renewal_failed', 'ok']  # most severe first
    layer = client.get(f'/v1/admin/layers/{layers.NON_PROD}/infrastructure', headers=as_('admin')).json()
    assert [c['key'] for c in layer['certificates']] == [f'layer:{layers.NON_PROD}:trino']

    assert client.get('/internal/metrics/certificates').status_code == 404  # disabled without a token
    monkeypatch.setattr(get_settings(), 'metrics_token', 'scrape-token')
    assert client.get('/internal/metrics/certificates', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    text = client.get('/internal/metrics/certificates', headers={'Authorization': 'Bearer scrape-token'}).text
    assert 'fdp_certificate_expiry_timestamp_seconds{certificate="gateway:public",scope="gateway",hostname="fdp.test"}' in text
    assert f'fdp_certificate_problem{{certificate="layer:{layers.NON_PROD}:trino",' in text and 'state="renewal_failed"} 1' in text


def test_controller_records_layer_certificates_and_forgets_them_on_retirement(client, tmp_path):
    from app import layer_controller as controller
    from app.layer_infrastructure import InfrastructureSpec
    leaf = pki(tmp_path, days=60)
    spec = InfrastructureSpec.model_validate({'enabled': True, 'trino': {'tls': {
        'mode': 'cert_manager', 'secret_name': 'trino-tls', 'issuer': {'name': 'layer-ca'}}}})
    from .test_layer_controller import WORKERS
    claim = controller.Claim('op', layers.NON_PROD, 1, 'token', spec, workers=WORKERS)

    class Resources:
        def served_certificate(self, hostname, port, postgres=False):
            assert (hostname, port, postgres) == ('fdp-trino.ns.svc', 8443, False)
            return leaf
    issued = {'fdp-trino': {'renewalTime': '2099-01-01T00:00:00Z', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
    controller.monitor_certificates(claim, Resources(), 'ns', issued, SessionLocal)
    with SessionLocal() as db:
        row = db.get(CertificateCheck, f'layer:{layers.NON_PROD}:trino')
        assert row.state == 'ok' and row.renewal_time.year == 2099
        cert_monitor.forget(db, layers.NON_PROD)
        db.commit()
        assert db.get(CertificateCheck, f'layer:{layers.NON_PROD}:trino') is None


def test_chart_watches_gateway_certificates_and_generates_a_metrics_token():
    from .test_helm import render, resource
    rendered = render('--set', 'tls.mode=existing')
    env = {e['name']: e.get('value') for e in
           resource(rendered, 'Deployment', 'fdp-control-plane')['spec']['template']['spec']['containers'][0]['env']}
    assert env['FDP_GATEWAY_TLS_ENDPOINTS'].startswith('public=fdp-traefik.')
    assert ':9443/' in env['FDP_GATEWAY_TLS_ENDPOINTS']
    off = render()
    env = {e['name'] for e in resource(off, 'Deployment', 'fdp-control-plane')['spec']['template']['spec']['containers'][0]['env']}
    assert 'FDP_GATEWAY_TLS_ENDPOINTS' not in env  # plain HTTP behind external TLS: nothing to check here
