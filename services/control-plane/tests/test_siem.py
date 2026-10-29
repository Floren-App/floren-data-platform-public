"""SIEM export: event format, hash chain and signatures, cursors and gaps, and every destination against a real
local receiver (syslog over TLS and UDP, HTTP collectors, OTLP). No cluster or external service is needed."""
import io
import json
import socket
import ssl
import subprocess
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app import siem
from app.config import Settings
from app.models import AuditEvent, QueryLog, SiemCursor, utcnow

ROOT = Path(__file__).resolve().parents[3]
VERIFY = ROOT / 'scripts/verify-audit-chain.py'


@pytest.fixture
def factory(client):
    from app.db import SessionLocal
    with SessionLocal() as db:   # start from an empty trail (startup records its own migration events)
        db.query(AuditEvent).delete()
        db.commit()
    return SessionLocal


def settings(**kw):
    base = dict(public_url='https://fdp.example.com', siem_gap_wait_seconds=60, siem_query_settle_seconds=0)
    return Settings(**{**base, **kw})


def add_audit(factory, *events, at=None):
    with factory() as db:
        for action, actor, target, detail in events:
            db.add(AuditEvent(actor=actor, action=action, target=target, detail=detail, **({'at': at} if at else {})))
        db.commit()


class Collect:
    name = 'stdout'

    def __init__(self):
        self.events, self.fail = [], False

    def send(self, events):
        if self.fail:
            raise siem.SinkError('cannot reach the SIEM collector')
        self.events += events


def verify(events, key=None):
    lines = ''.join(json.dumps(e) + '\n' for e in events)
    env = {'FDP_SIEM_SIGNING_KEY': key} if key else {}
    return subprocess.run([sys.executable, str(VERIFY)], input=lines, capture_output=True, text=True, env=env)


def test_events_carry_a_verifiable_signed_chain_and_tampering_is_detected(factory):
    add_audit(factory, ('secret.create', 'ana', 'env/user/ana/token', {}),
              ('rbac.assign', 'admin', 'schema-1', {'role': 'Reader', 'group': 'analysts'}),
              ('api.call_denied', 'service:crm', 'crm/customers', {}))
    sink, cfg = Collect(), settings(siem_signing_key='k' * 32)
    assert siem.export_once(factory, sink, cfg) == 3
    first = sink.events[0]
    assert first['schema'] == 'fdp.audit/1' and first['id'].startswith('audit:') and first['installation'] == 'fdp.example.com'
    assert first['category'] == 'secret' and first['time'].endswith('Z') and first['chain']['prev'] == siem.GENESIS
    assert sink.events[2]['outcome'] == 'failure' and sink.events[1]['outcome'] == 'success'
    assert [e['chain']['seq'] for e in sink.events] == [1, 2, 3]
    assert verify(sink.events, 'k' * 32).returncode == 0
    assert verify(sink.events, 'wrong-key').returncode == 1
    tampered = json.loads(json.dumps(sink.events))
    tampered[1]['detail']['role'] = 'Owner'
    result = verify(tampered)
    assert result.returncode == 1 and 'altered' in result.stderr
    assert 'missing' in verify([sink.events[0], sink.events[2]]).stderr


def test_the_chain_continues_across_batches_and_failures_resend_without_loss(factory):
    cfg = settings(siem_batch_size=2)
    add_audit(factory, *[('workload.deploy', 'ana', f'w{i}', {}) for i in range(5)])
    sink = Collect()
    assert siem.export_once(factory, sink, cfg) == 2
    sink.fail = True
    with pytest.raises(siem.SinkError):
        siem.export_once(factory, sink, cfg)
    with factory() as db:
        cursor = db.get(SiemCursor, 'stdout')
        assert cursor.audit_id == int(sink.events[-1]['id'].split(':')[1]) and cursor.last_error
    sink.fail = False
    while siem.export_once(factory, sink, cfg):
        pass
    assert [e['target'] for e in sink.events] == [f'w{i}' for i in range(5)]
    assert verify(sink.events).returncode == 0
    with factory() as db:
        assert db.get(SiemCursor, 'stdout').last_error == ''


def test_an_id_gap_waits_for_a_slower_commit_then_is_skipped(factory):
    add_audit(factory, ('a.one', 'x', 't', {}), ('a.two', 'x', 't', {}), ('a.three', 'x', 't', {}))
    with factory() as db:   # simulate id 2 still uncommitted: remove it, so ids 1 and 3 remain
        db.delete(db.get(AuditEvent, 2))
        db.commit()
    sink, cfg = Collect(), settings()
    assert siem.export_once(factory, sink, cfg) == 1   # stops before the gap
    assert siem.export_once(factory, sink, cfg) == 0
    later = utcnow() + timedelta(seconds=61)
    assert siem.export_once(factory, sink, cfg, now=later) == 1   # the gap is permanent now
    assert [e['action'] for e in sink.events] == ['a.one', 'a.three']


def test_data_access_events_come_from_the_query_log_without_sql_text_by_default(factory):
    with factory() as db:
        db.add(QueryLog(layer_id='1a0000000001', query_id='20261029_1', actor='ana', principal_id='p1', state='FINISHED',
                        tables=['sales.public.orders'], rows=42, sql_text='select * from orders where id = 7',
                        ended_at=utcnow()))
        db.add(QueryLog(layer_id='1a0000000001', query_id='20261029_2', actor='svc', state='FAILED', error='denied',
                        tables=['ledger.public.entries']))
        db.commit()
    sink = Collect()
    siem.export_once(factory, sink, settings())
    queries = [e for e in sink.events if e['source'] == 'query']
    assert {e['outcome'] for e in queries} == {'success', 'failure'}
    ok = next(e for e in queries if e['outcome'] == 'success')
    assert ok['action'] == 'sql.query' and ok['detail']['tables_read'] == ['sales.public.orders'] and 'sql' not in ok['detail']
    sink2 = Collect(); sink2.name = 'http'
    siem.export_once(factory, sink2, settings(siem_include_sql_text=True))
    assert any(e['detail'].get('sql') for e in sink2.events)
    sink3 = Collect(); sink3.name = 'otlp'
    siem.export_once(factory, sink3, settings(siem_include_queries=False))
    assert not [e for e in sink3.events if e['source'] == 'query']


def test_a_new_destination_can_start_from_now(factory):
    add_audit(factory, ('old.event', 'x', 't', {}))
    sink = Collect()
    assert siem.export_once(factory, sink, settings(siem_start='now')) == 0
    add_audit(factory, ('new.event', 'x', 't', {}))
    siem.export_once(factory, sink, settings(siem_start='now'))
    assert [e['action'] for e in sink.events] == ['new.event']


def test_stdout_writes_one_json_object_per_line(factory):
    add_audit(factory, ('secret.create', 'ana', 'x', {}))
    stream = io.StringIO()
    siem.export_once(factory, siem.StdoutSink(settings(), stream=stream), settings())
    line = stream.getvalue().strip().splitlines()
    assert len(line) == 1 and json.loads(line[0])['action'] == 'secret.create'


# --- Real receivers ------------------------------------------------------------------------------------------

def tls_material(tmp_path):
    key, cert = tmp_path / 'server.key', tmp_path / 'server.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True, capture_output=True)
    return cert, key


def test_syslog_over_tls_frames_rfc5424_messages_and_verifies_the_receiver(factory, tmp_path, monkeypatch):
    cert, key = tls_material(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = socket.create_server(('127.0.0.1', 0))
    port, received = server.getsockname()[1], []

    def accept():
        conn, _ = server.accept()
        with context.wrap_socket(conn, server_side=True) as tls:
            data = b''
            while chunk := tls.recv(65536):
                data += chunk
            received.append(data)
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    add_audit(factory, ('secret.create', 'ana', 'x', {}), ('api.call_denied', 'svc', 'y', {}))
    sink = siem.SyslogSink(settings(siem_syslog_address=f'localhost:{port}', siem_syslog_ca_file=str(cert)))
    siem.export_once(factory, sink, settings())
    thread.join(5)
    data, messages = received[0], []
    while data:   # octet counting: "<length> <message>"
        length, _, rest = data.partition(b' ')
        messages.append(rest[:int(length)])
        data = rest[int(length):]
    assert len(messages) == 2
    assert messages[0].startswith(b'<110>1 ') and b' fdp.example.com fdp - secret.create - {' in messages[0]
    assert messages[1].startswith(b'<108>1 ')   # facility 13, warning for a failure
    assert json.loads(messages[0].split(b' - ', 2)[2])['action'] == 'secret.create'
    server.close()


def test_syslog_refuses_an_untrusted_receiver_with_a_sanitized_error(factory, tmp_path, monkeypatch):
    cert, key = tls_material(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = socket.create_server(('127.0.0.1', 0))
    port = server.getsockname()[1]

    def accept():
        conn, _ = server.accept()
        try:
            context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            pass
    threading.Thread(target=accept, daemon=True).start()
    add_audit(factory, ('secret.create', 'ana', 'x', {}))
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    with pytest.raises(siem.SinkError, match='not trusted'):
        siem.export_once(factory, siem.SyslogSink(settings(siem_syslog_address=f'127.0.0.1:{port}')), settings())
    with factory() as db:
        assert 'not trusted' in db.get(SiemCursor, 'syslog').last_error and str(port) not in db.get(SiemCursor, 'syslog').last_error
    server.close()


def test_syslog_over_udp_sends_one_datagram_per_event(factory):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(('127.0.0.1', 0))
    receiver.settimeout(5)
    add_audit(factory, ('secret.create', 'ana', 'x', {}))
    siem.export_once(factory, siem.SyslogSink(settings(siem_syslog_address=f'127.0.0.1:{receiver.getsockname()[1]}',
                                                       siem_syslog_transport='udp')), settings())
    assert receiver.recv(65536).startswith(b'<110>1 ')
    receiver.close()


class Collector(BaseHTTPRequestHandler):
    requests, status, reply = [], 200, b'{}'

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        Collector.requests.append((self.path, dict(self.headers), body))
        self.send_response(Collector.status)
        self.send_header('Content-Length', str(len(Collector.reply)))
        self.end_headers()
        self.wfile.write(Collector.reply)


@pytest.fixture
def collector():
    Collector.requests, Collector.status, Collector.reply = [], 200, b'{}'
    server = HTTPServer(('127.0.0.1', 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()


@pytest.mark.parametrize('fmt,path,auth', [('generic', '/ingest', 'Bearer t0k'), ('splunk', '/services/collector/event', 'Splunk t0k'),
                                           ('elastic', '/_bulk', 'ApiKey t0k')])
def test_https_collector_formats(factory, collector, fmt, path, auth):
    add_audit(factory, ('secret.create', 'ana', 'x', {}), ('rbac.assign', 'admin', 'y', {}))
    url = collector + ('/ingest' if fmt == 'generic' else '/services/collector/event' if fmt == 'splunk' else '')
    Collector.reply = b'{"errors":false}' if fmt == 'elastic' else b'{}'
    siem.export_once(factory, siem.HttpSink(settings(siem_http_url=url, siem_http_format=fmt, siem_http_token='t0k')), settings())
    got_path, headers, body = Collector.requests[0]
    assert got_path == path and headers['Authorization'] == auth
    lines = [json.loads(l) for l in body.decode().strip().splitlines()]
    if fmt == 'generic':
        events = lines
    elif fmt == 'splunk':
        assert lines[0]['sourcetype'] == 'fdp:audit' and lines[0]['host'] == 'fdp.example.com'
        events = [l['event'] for l in lines]
    else:
        assert lines[0] == {'create': {'_index': 'fdp-audit'}} and '@timestamp' in lines[1]
        events = lines[1::2]
    assert [e['action'] for e in events] == ['secret.create', 'rbac.assign']
    assert verify([json.loads(l) for l in body.decode().strip().splitlines()][1::2] if fmt == 'elastic' else
                  [json.loads(l) for l in body.decode().strip().splitlines()]).returncode == 0


def test_https_collector_rejections_keep_the_events_for_the_next_attempt(factory, collector):
    add_audit(factory, ('secret.create', 'ana', 'x', {}))
    sink = siem.HttpSink(settings(siem_http_url=collector, siem_http_token='secret-token'))
    Collector.status = 403
    with pytest.raises(siem.SinkError, match='refused the credentials'):
        siem.export_once(factory, sink, settings())
    Collector.status = 200
    assert siem.export_once(factory, sink, settings()) == 1
    with factory() as db:
        assert 'secret-token' not in (db.get(SiemCursor, 'http').last_error or '')
    elastic = siem.HttpSink(settings(siem_http_url=collector, siem_http_format='elastic'))
    elastic.name = 'elastic-test'
    Collector.reply = b'{"errors":true}'
    with pytest.raises(siem.SinkError, match='item errors'):
        siem.export_once(factory, elastic, settings())


def test_otlp_logs_carry_attributes_and_the_full_event(factory, collector):
    add_audit(factory, ('api.call_denied', 'svc', 'crm', {}))
    siem.export_once(factory, siem.OtlpSink(settings(siem_otlp_endpoint=collector, siem_otlp_headers='x-api-key=abc, x-tenant = fdp')),
                     settings())
    path, headers, body = Collector.requests[0]
    assert path == '/v1/logs' and headers['x-api-key'] == 'abc' and headers['x-tenant'] == 'fdp'
    record = json.loads(body)['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
    attrs = {a['key']: a['value']['stringValue'] for a in record['attributes']}
    assert record['severityText'] == 'WARN' and attrs['event.name'] == 'api.call_denied' and attrs['fdp.outcome'] == 'failure'
    assert json.loads(record['body']['stringValue'])['chain']['seq'] == 1


def test_admin_status_reports_destinations_without_secrets(client, factory, monkeypatch):
    from .conftest import as_
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'siem_http_url', 'https://siem.internal/ingest')
    monkeypatch.setattr(get_settings(), 'siem_http_token', 'secret-token')
    add_audit(factory, ('secret.create', 'ana', 'x', {}))
    sink = Collect(); sink.name = 'http'
    siem.export_once(factory, sink, settings())
    body = client.get('/v1/admin/siem', headers=as_('admin')).json()
    http = next(d for d in body['destinations'] if d['sink'] == 'http')
    assert http['configured'] is True and http['events_sent'] == 1 and http['last_error'] is None
    assert 'secret-token' not in json.dumps(body) and 'siem.internal' not in json.dumps(body)
    assert client.get('/v1/admin/siem', headers=as_('ana')).status_code == 403
