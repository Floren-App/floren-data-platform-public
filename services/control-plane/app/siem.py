"""Stream the audit trail and data-access events to SIEM systems.

Sources: ``audit_events`` (administration, permissions, secrets, API calls, ...) and, optionally, ``query_log``
(every completed SQL query: identity, tables, rows, outcome). Destinations: JSON lines on stdout, syslog
(RFC 5424 over TLS, TCP or UDP), an HTTPS collector (generic JSON lines, Splunk HEC or Elastic bulk) and
OpenTelemetry logs (OTLP/HTTP).

Each destination has its own cursor and hash chain in ``siem_cursors``: every event carries the hash of the
previous one, so a missing, reordered or altered event is detectable at the receiver
(``scripts/verify-audit-chain.py``). With a signing key, each chain hash is also HMAC-signed. Delivery is
at least once: a destination may receive a batch again after a failure; the event ``id`` identifies duplicates.
Runs only on the leader replica. Errors are sanitized: no URLs, tokens or response bodies are stored or logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import socket
import ssl
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select

from .config import Settings, get_settings
from .models import AuditEvent, QueryLog, SiemCursor, utcnow

log = logging.getLogger(__name__)
SCHEMA = 'fdp.audit/1'
GENESIS = '0' * 64


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def canonical(event: dict) -> bytes:
    return json.dumps(event, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()


def chain(event: dict, seq: int, prev: str, key: Optional[str]) -> dict:
    """Add the hash chain (and signature) to an event without them."""
    digest = hashlib.sha256(prev.encode() + canonical(event)).hexdigest()
    out = {**event, 'chain': {'seq': seq, 'prev': prev, 'hash': digest}}
    if key:
        out['signature'] = hmac.new(key.encode(), digest.encode(), hashlib.sha256).hexdigest()
    return out


def installation(settings: Settings) -> str:
    return urlsplit(settings.public_url).hostname or 'fdp'


def audit_event(row: AuditEvent, settings: Settings) -> dict:
    detail = dict(row.detail or {})
    failed = any(word in row.action for word in ('denied', 'fail', 'refused', 'unavailable'))
    outcome = detail.pop('outcome', None) or ('failure' if failed else 'success')
    return {'schema': SCHEMA, 'id': f'audit:{row.id}', 'time': _iso(row.at), 'installation': installation(settings),
            'source': 'audit', 'category': row.action.split('.', 1)[0], 'action': row.action, 'outcome': outcome,
            'actor': row.actor, 'target': row.target, 'detail': detail}


def query_event(row: QueryLog, settings: Settings) -> dict:
    detail = {'layer_id': row.layer_id, 'engine_query_id': row.query_id, 'query_type': row.query_type,
              'source': row.source, 'tables_read': list(row.tables or []), 'table_written': row.output_table or None,
              'rows': row.rows, 'bytes': row.bytes, 'wall_ms': row.wall_ms, 'run_id': row.run_id or None,
              'workload_id': row.workload_id or None, 'workspace_id': row.workspace_id or None,
              'principal_id': row.principal_id or None, 'initiator_id': row.initiator_id or None}
    if row.error:
        detail['error'] = row.error
    if settings.siem_include_sql_text and row.sql_text:
        detail['sql'] = row.sql_text
    return {'schema': SCHEMA, 'id': f'query:{row.id}', 'time': _iso(row.ended_at or row.created_at),
            'installation': installation(settings), 'source': 'query', 'category': 'data', 'action': 'sql.query',
            'outcome': 'success' if row.state == 'FINISHED' else 'failure', 'actor': row.actor or row.trino_user,
            'target': ','.join(row.tables or []) or row.output_table or '', 'detail': {k: v for k, v in detail.items() if v is not None}}


class SinkError(Exception):
    """A sanitized delivery failure."""


# --- Destinations ------------------------------------------------------------------------------------------


class StdoutSink:
    name = 'stdout'

    def __init__(self, settings: Settings, stream=None):
        self.stream = stream or sys.stdout

    def send(self, events: List[dict]) -> None:
        self.stream.write(''.join(json.dumps(e, separators=(',', ':'), ensure_ascii=False, default=str) + '\n'
                                  for e in events))
        self.stream.flush()


class SyslogSink:
    """RFC 5424 messages; octet-counting framing on TCP and TLS (RFC 5425, RFC 6587), one datagram per event on UDP."""

    name = 'syslog'

    def __init__(self, settings: Settings):
        host, _, port = settings.siem_syslog_address.rpartition(':')
        if not host or not port.isdigit():
            raise ValueError('siem_syslog_address must be host:port')
        self.host, self.port = host.strip('[]'), int(port)
        self.transport, self.facility = settings.siem_syslog_transport, settings.siem_syslog_facility
        self.hostname = installation(settings)
        self.context = None
        if self.transport == 'tls':
            self.context = ssl.create_default_context()
            # The receiver's CA: explicit, or the platform trust bundle the chart mounts (SSL_CERT_FILE).
            ca = settings.siem_syslog_ca_file or os.environ.get('SSL_CERT_FILE')
            if ca:
                self.context.load_verify_locations(cafile=ca)
            if settings.siem_syslog_client_cert_file:
                self.context.load_cert_chain(settings.siem_syslog_client_cert_file, settings.siem_syslog_client_key_file)

    def message(self, event: dict) -> bytes:
        severity = 4 if event.get('outcome') == 'failure' else 6
        msgid = ''.join(c for c in event['action'] if 33 <= ord(c) <= 126)[:32] or '-'
        header = f"<{self.facility * 8 + severity}>1 {event['time']} {self.hostname[:255]} fdp - {msgid} - "
        return header.encode() + json.dumps(event, separators=(',', ':'), ensure_ascii=False, default=str).encode()

    def send(self, events: List[dict]) -> None:
        messages = [self.message(e) for e in events]
        try:
            if self.transport == 'udp':
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    for m in messages:
                        sock.sendto(m, (self.host, self.port))
                return
            with socket.create_connection((self.host, self.port), timeout=15) as raw:
                sock = self.context.wrap_socket(raw, server_hostname=self.host) if self.context else raw
                try:
                    sock.sendall(b''.join(str(len(m)).encode() + b' ' + m for m in messages))
                finally:
                    if sock is not raw:
                        sock.close()
        except ssl.SSLCertVerificationError:
            raise SinkError('the syslog receiver certificate is not trusted') from None
        except ssl.SSLError:
            raise SinkError('the TLS handshake with the syslog receiver failed') from None
        except OSError:
            raise SinkError('cannot reach the syslog receiver') from None


class HttpSink:
    """An HTTPS collector: generic JSON lines, Splunk HTTP Event Collector, or the Elastic bulk API."""

    name = 'http'

    def __init__(self, settings: Settings, client=None):
        import httpx
        self.url, self.format, self.index = settings.siem_http_url, settings.siem_http_format, settings.siem_http_index
        if urlsplit(self.url).scheme not in ('https', 'http'):
            raise ValueError('siem_http_url must be an http(s) URL')
        token = settings.siem_http_token
        scheme = {'generic': 'Bearer', 'splunk': 'Splunk', 'elastic': 'ApiKey'}[self.format]
        self.headers = {'Authorization': f'{scheme} {token}'} if token else {}
        self.hostname = installation(settings)
        self.client = client or httpx.Client(timeout=20)

    def body(self, events: List[dict]) -> Tuple[str, bytes]:
        dump = lambda value: json.dumps(value, separators=(',', ':'), ensure_ascii=False, default=str)
        if self.format == 'splunk':
            lines = [dump({'time': datetime.fromisoformat(e['time'].replace('Z', '+00:00')).timestamp(), 'host': self.hostname,
                           'source': 'fdp', 'sourcetype': 'fdp:audit', 'event': e}) for e in events]
            return 'application/json', '\n'.join(lines).encode()
        if self.format == 'elastic':
            lines = []
            for e in events:
                lines += [dump({'create': {'_index': self.index}}), dump({'@timestamp': e['time'], **e})]
            return 'application/x-ndjson', ('\n'.join(lines) + '\n').encode()
        return 'application/x-ndjson', ('\n'.join(dump(e) for e in events) + '\n').encode()

    def send(self, events: List[dict]) -> None:
        import httpx
        content_type, data = self.body(events)
        url = self.url.rstrip('/') + '/_bulk' if self.format == 'elastic' else self.url
        try:
            response = self.client.post(url, content=data, headers={**self.headers, 'Content-Type': content_type})
        except httpx.HTTPError:
            raise SinkError('cannot reach the SIEM collector') from None
        if response.status_code in (401, 403):
            raise SinkError(f'the SIEM collector refused the credentials (HTTP {response.status_code})')
        if response.status_code >= 300:
            raise SinkError(f'the SIEM collector rejected the batch (HTTP {response.status_code})')
        if self.format == 'elastic':
            try:
                failed = response.json().get('errors')
            except ValueError:
                failed = True
            if failed:
                raise SinkError('the Elastic bulk request reported item errors')


class OtlpSink:
    """OpenTelemetry logs over OTLP/HTTP with JSON encoding, to an OpenTelemetry Collector or compatible backend."""

    name = 'otlp'

    def __init__(self, settings: Settings, client=None):
        import httpx
        self.url = settings.siem_otlp_endpoint.rstrip('/') + '/v1/logs'
        if urlsplit(self.url).scheme not in ('https', 'http'):
            raise ValueError('siem_otlp_endpoint must be an http(s) URL')
        self.headers = {}
        for part in (settings.siem_otlp_headers or '').split(','):
            key, sep, value = part.partition('=')
            if sep and key.strip():
                self.headers[key.strip()] = value.strip()
        self.installation = installation(settings)
        self.client = client or httpx.Client(timeout=20)

    @staticmethod
    def _value(value) -> dict:
        return {'stringValue': value if isinstance(value, str) else json.dumps(value, default=str)}

    def body(self, events: List[dict]) -> dict:
        records = []
        for e in events:
            nanos = str(int(datetime.fromisoformat(e['time'].replace('Z', '+00:00')).timestamp() * 1_000_000_000))
            failure = e.get('outcome') == 'failure'
            attributes = {'event.name': e['action'], 'fdp.event.id': e['id'], 'fdp.category': e['category'],
                          'fdp.outcome': e['outcome'], 'fdp.actor': e['actor'], 'fdp.target': e['target'],
                          'fdp.chain.hash': e['chain']['hash']}
            records.append({'timeUnixNano': nanos, 'severityNumber': 13 if failure else 9,
                            'severityText': 'WARN' if failure else 'INFO',
                            'body': {'stringValue': json.dumps(e, separators=(',', ':'), ensure_ascii=False, default=str)},
                            'attributes': [{'key': k, 'value': self._value(v)} for k, v in attributes.items()]})
        resource = [{'key': 'service.name', 'value': {'stringValue': 'fdp-control-plane'}},
                    {'key': 'fdp.installation', 'value': {'stringValue': self.installation}}]
        return {'resourceLogs': [{'resource': {'attributes': resource},
                                  'scopeLogs': [{'scope': {'name': 'fdp.audit'}, 'logRecords': records}]}]}

    def send(self, events: List[dict]) -> None:
        import httpx
        try:
            response = self.client.post(self.url, json=self.body(events), headers=self.headers)
        except httpx.HTTPError:
            raise SinkError('cannot reach the OpenTelemetry collector') from None
        if response.status_code >= 300:
            raise SinkError(f'the OpenTelemetry collector rejected the batch (HTTP {response.status_code})')


def sinks(settings: Settings) -> list:
    out = []
    if settings.siem_stdout:
        out.append(StdoutSink(settings))
    if settings.siem_syslog_address:
        out.append(SyslogSink(settings))
    if settings.siem_http_url:
        out.append(HttpSink(settings))
    if settings.siem_otlp_endpoint:
        out.append(OtlpSink(settings))
    return out


# --- Export ------------------------------------------------------------------------------------------------


def _cursor(db, sink: str, settings: Settings) -> SiemCursor:
    cursor = db.get(SiemCursor, sink)
    if cursor is None:
        cursor = SiemCursor(sink=sink, audit_id=0, seq=0, last_hash=GENESIS, query_id='')
        if settings.siem_start == 'now':
            from sqlalchemy import func
            cursor.audit_id = db.scalar(select(func.max(AuditEvent.id))) or 0
            cursor.query_at = utcnow()
        db.add(cursor)
        db.flush()
    return cursor


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def pending_audit(db, cursor: SiemCursor, settings: Settings, now: datetime) -> List[AuditEvent]:
    """Audit rows after the cursor, in id order, stopping at a gap that a slower transaction may still fill."""
    rows = db.scalars(select(AuditEvent).where(AuditEvent.id > cursor.audit_id).order_by(AuditEvent.id)
                      .limit(settings.siem_batch_size)).all()
    out, expected = [], cursor.audit_id + 1
    for row in rows:
        if row.id != expected and now - _aware(row.at) < timedelta(seconds=settings.siem_gap_wait_seconds):
            break   # ids before this one may still commit; a rolled-back id becomes a permanent gap after the wait
        out.append(row)
        expected = row.id + 1
    return out


def pending_queries(db, cursor: SiemCursor, settings: Settings, now: datetime, limit: int) -> List[QueryLog]:
    if not settings.siem_include_queries or limit <= 0:
        return []
    settled = now - timedelta(seconds=settings.siem_query_settle_seconds)
    after = []
    if cursor.query_at is not None:
        at = cursor.query_at
        after = [or_(QueryLog.created_at > at, and_(QueryLog.created_at == at, QueryLog.id > cursor.query_id))]
    return db.scalars(select(QueryLog).where(QueryLog.created_at <= settled, *after)
                      .order_by(QueryLog.created_at, QueryLog.id).limit(limit)).all()


def export_once(factory, sink, settings: Settings, now: Optional[datetime] = None) -> int:
    """Deliver one batch to one destination; the cursor and chain advance only after it was accepted."""
    now = now or utcnow()
    with factory() as db:
        cursor = _cursor(db, sink.name, settings)
        audits = pending_audit(db, cursor, settings, now)
        queries = pending_queries(db, cursor, settings, now, settings.siem_batch_size - len(audits))
        rows = [(r.at, 0, audit_event(r, settings), r) for r in audits] + \
               [(r.created_at, 1, query_event(r, settings), r) for r in queries]
        if not rows:
            db.commit()
            return 0
        events, seq, prev = [], cursor.seq, cursor.last_hash or GENESIS
        for _, _, event, _ in sorted(rows, key=lambda item: (_aware(item[0]), item[1])):
            seq += 1
            event = chain(event, seq, prev, settings.siem_signing_key)
            prev = event['chain']['hash']
            events.append(event)
        try:
            sink.send(events)
        except SinkError as exc:
            cursor.last_error, cursor.last_error_at = str(exc)[:300], now
            db.commit()
            raise
        except Exception as exc:   # never store or log details that could contain endpoints or credentials
            cursor.last_error, cursor.last_error_at = f'delivery failed ({type(exc).__name__})', now
            db.commit()
            raise SinkError(cursor.last_error) from None
        if audits:
            cursor.audit_id = audits[-1].id
        if queries:
            cursor.query_at, cursor.query_id = queries[-1].created_at, queries[-1].id
        cursor.seq, cursor.last_hash = seq, prev
        cursor.last_success_at, cursor.last_error = now, ''
        db.commit()
        return len(events)


def status(db, settings: Settings) -> List[Dict]:
    """Per destination: whether it is configured, how far it is and its last sanitized error."""
    configured = {s.name for s in sinks_safe(settings)}
    rows = {c.sink: c for c in db.scalars(select(SiemCursor))}
    out = []
    for name in ('stdout', 'syslog', 'http', 'otlp'):
        c = rows.get(name)
        if name not in configured and c is None:
            continue
        exported = None
        if c is not None and c.audit_id:
            row = db.get(AuditEvent, c.audit_id)
            exported = _iso(row.at) if row else None
        out.append({'sink': name, 'configured': name in configured, 'events_sent': c.seq if c else 0,
                    'exported_through': exported, 'last_success_at': _iso(c.last_success_at) if c else None,
                    'last_error': (c.last_error or None) if c else None,
                    'last_error_at': _iso(c.last_error_at) if c and c.last_error else None})
    return out


def sinks_safe(settings: Settings) -> list:
    try:
        return sinks(settings)
    except (ValueError, OSError):
        return []


async def siem_loop(factory, settings: Optional[Settings] = None):
    settings = settings or get_settings()
    try:
        destinations = sinks(settings)
    except (ValueError, OSError) as exc:
        log.error('SIEM export is misconfigured: %s', type(exc).__name__ if isinstance(exc, OSError) else exc)
        return
    if not destinations:
        return
    while True:
        for sink in destinations:
            try:
                # Drain the backlog in batches, then wait for the next interval.
                while await asyncio.to_thread(export_once, factory, sink, settings) >= settings.siem_batch_size:
                    pass
            except SinkError as exc:
                log.warning('SIEM export to %s failed: %s', sink.name, exc)
            except Exception:
                log.exception('SIEM export to %s failed', sink.name)
        await asyncio.sleep(settings.siem_interval_seconds)
