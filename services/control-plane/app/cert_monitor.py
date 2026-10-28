"""Certificate expiry and renewal monitoring: the states operators alert on.

Layer certificates are recorded by the layer controller: the certificate its verified TLS
handshake was served, plus cert-manager's status when cert-manager issues it. Gateway certificates
are recorded by the control plane the same way. No Secret or private key is ever read.

States, by severity: ok and renewing (0); expiring, renewal_failed, renewal_overdue and
unverified (1, warning); expired (2, critical).
"""
from __future__ import annotations

import logging
import socket
import ssl
import struct
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import delete, select

from .models import CertificateCheck, utcnow

# Certificates without a managed renewal schedule warn this long before expiry.
WARNING = timedelta(days=14)
# cert-manager renews at status.renewalTime; this much later without a new certificate is overdue.
RENEWAL_GRACE = timedelta(hours=1)
SEVERITY = {'ok': 0, 'renewing': 0, 'expiring': 1, 'renewal_failed': 1, 'renewal_overdue': 1,
            'unverified': 1, 'expired': 2}
log = logging.getLogger(__name__)


def served_certificate(host: str, port: int, server_name: Optional[str] = None, postgres: bool = False,
                       timeout: float = 5):
    """The certificate a verified TLS handshake receives (chain and hostname checked), or None."""
    from cryptography import x509
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as transport:
            if postgres:
                transport.sendall(struct.pack('!II', 8, 80877103))  # PostgreSQL SSLRequest
                if transport.recv(1) != b'S':
                    return None
            with context.wrap_socket(transport, server_hostname=server_name or host) as secured:
                return x509.load_der_x509_certificate(secured.getpeercert(binary_form=True))
    except (OSError, ValueError):
        return None


def _time(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def issuing_timeout() -> timedelta:
    from .config import get_settings
    return timedelta(seconds=get_settings().certificate_issuing_timeout_seconds)


def manager_status(status: Optional[dict], now: Optional[datetime] = None) -> dict:
    """The parts of a cert-manager Certificate status that matter for alerting.

    An issuer that is not ready leaves a renewal pending without any failure condition, so an
    issuance in progress longer than the timeout counts as failed too."""
    status = status or {}
    now = now or utcnow()
    conditions = {c.get('type'): c for c in status.get('conditions', []) if isinstance(c, dict)}
    ready = conditions.get('Ready', {})
    issuing = conditions.get('Issuing', {})
    failure = None
    if status.get('lastFailureTime') or (issuing.get('status') == 'False' and issuing.get('reason') == 'Failed'):
        failure = (issuing.get('message') or ready.get('message') or 'issuance failed').strip()[:300]
    elif issuing.get('status') == 'True' and _time(issuing.get('lastTransitionTime')) and \
            now - _time(issuing['lastTransitionTime']) > issuing_timeout():
        failure = ('issuance pending since %s; check the issuer and its CertificateRequests'
                   % _time(issuing['lastTransitionTime']).isoformat())
    return {'ready': ready.get('status') == 'True' if ready else None, 'failure': failure,
            'renewal_time': _time(status.get('renewalTime')), 'not_after': _time(status.get('notAfter'))}


def classify(now: datetime, not_after: Optional[datetime], renewal_time: Optional[datetime] = None,
             ready: Optional[bool] = None, failure: Optional[str] = None) -> Tuple[str, str]:
    if not_after is None:
        return 'unverified', 'The endpoint did not present a certificate trusted by the platform CA bundle'
    if not_after <= now:
        return 'expired', f'Expired at {not_after.isoformat()}'
    left = f'expires {not_after.isoformat()}'
    if failure:
        return 'renewal_failed', f'Renewal failed ({failure}); the current certificate {left}'
    if renewal_time and now > renewal_time + RENEWAL_GRACE:
        return 'renewal_overdue', f'Renewal was due {renewal_time.isoformat()} and has not happened; {left}'
    if ready is False:
        return 'renewing', f'cert-manager is issuing a new certificate; the current one {left}'
    if renewal_time is None and not_after - now < WARNING:
        return 'expiring', f'Expires {not_after.isoformat()} and nothing renews it automatically; replace it'
    return 'ok', (f'Renews {renewal_time.isoformat()}; ' if renewal_time else '') + left[0].upper() + left[1:]


def record(db, key: str, scope: str, hostname: str, served=None, status: Optional[dict] = None,
           layer_id: Optional[str] = None, now: Optional[datetime] = None) -> CertificateCheck:
    """Upsert the current observation of one endpoint's certificate."""
    now = now or utcnow()
    managed = manager_status(status, now) if status is not None else {}
    not_after = served.not_valid_after_utc if served is not None else None
    state, message = classify(now, not_after, managed.get('renewal_time'), managed.get('ready'), managed.get('failure'))
    row = db.get(CertificateCheck, key) or CertificateCheck(key=key, scope=scope, changed_at=now, state='')
    if row.state != state:
        row.changed_at = now
        (log.warning if SEVERITY[state] else log.info)('certificate %s: %s (%s)', key, state, message)
    row.scope, row.layer_id, row.hostname, row.state, row.message = scope, layer_id, hostname, state, message
    row.not_after, row.renewal_time, row.checked_at = not_after, managed.get('renewal_time'), now
    row.serial = format(served.serial_number, 'x') if served is not None else None
    row.issuer = served.issuer.rfc4514_string()[:256] if served is not None else None
    db.add(row)
    return row


def forget(db, layer_id: Optional[str] = None, keep=()):
    """Drop checks of a layer's endpoints that no longer exist (all of them when keep is empty)."""
    statement = delete(CertificateCheck).where(CertificateCheck.layer_id == layer_id)
    if keep:
        statement = statement.where(CertificateCheck.key.not_in(list(keep)))
    db.execute(statement)


def out(row: CertificateCheck) -> dict:
    return {'key': row.key, 'scope': row.scope, 'layer_id': row.layer_id, 'hostname': row.hostname,
            'state': row.state, 'severity': SEVERITY.get(row.state, 1), 'message': row.message,
            'not_after': row.not_after, 'renewal_time': row.renewal_time, 'serial': row.serial,
            'issuer': row.issuer, 'checked_at': row.checked_at, 'changed_at': row.changed_at}


def rows(db, layer_id: Optional[str] = None):
    statement = select(CertificateCheck).order_by(CertificateCheck.key)
    if layer_id:
        statement = statement.where(CertificateCheck.layer_id == layer_id)
    return list(db.scalars(statement))


def metrics(checks) -> str:
    """Prometheus text exposition for alerting rules."""
    def labels(row, **extra):
        pairs = {'certificate': row.key, 'scope': row.scope, 'hostname': row.hostname, **extra}
        return ','.join('%s="%s"' % (k, str(v).replace('\\', '\\\\').replace('"', '\\"')) for k, v in pairs.items())
    lines = ['# HELP fdp_certificate_expiry_timestamp_seconds When the served certificate expires.',
             '# TYPE fdp_certificate_expiry_timestamp_seconds gauge']
    lines += ['fdp_certificate_expiry_timestamp_seconds{%s} %d' % (labels(r), r.not_after.timestamp())
              for r in checks if r.not_after]
    lines += ['# HELP fdp_certificate_problem 1 when the certificate needs attention, by state.',
              '# TYPE fdp_certificate_problem gauge']
    lines += ['fdp_certificate_problem{%s} %d' % (labels(r, state=r.state), 1 if SEVERITY.get(r.state, 1) else 0)
              for r in checks]
    lines += ['# HELP fdp_certificate_checked_timestamp_seconds When the certificate was last checked.',
              '# TYPE fdp_certificate_checked_timestamp_seconds gauge']
    lines += ['fdp_certificate_checked_timestamp_seconds{%s} %d' % (labels(r), r.checked_at.timestamp())
              for r in checks if r.checked_at]
    return '\n'.join(lines) + '\n'


def gateway_endpoints(setting: str):
    """FDP_GATEWAY_TLS_ENDPOINTS: "name=host:port/server-name,..." (chart-generated)."""
    result = []
    for item in filter(None, (x.strip() for x in (setting or '').split(','))):
        name, _, target = item.partition('=')
        address, _, server_name = target.partition('/')
        host, _, port = address.rpartition(':')
        result.append((name, host, int(port), server_name or host))
    return result


def check_gateways(factory, setting: str, now: Optional[datetime] = None) -> int:
    """Record each gateway certificate; returns the worst severity seen."""
    worst = 0
    for name, host, port, server_name in gateway_endpoints(setting):
        served = served_certificate(host, port, server_name)
        with factory() as db:
            worst = max(worst, SEVERITY[record(db, 'gateway:' + name, 'gateway', server_name, served=served, now=now).state])
            db.commit()
    return worst


async def gateway_loop(factory, setting: str, interval: float):
    import asyncio
    from starlette.concurrency import run_in_threadpool
    while True:
        worst = 1
        try:
            worst = await run_in_threadpool(check_gateways, factory, setting)
        except Exception:
            log.exception('gateway certificate check failed; retrying next interval')
        # Look again soon while something is wrong (e.g. the gateway is still starting).
        await asyncio.sleep(max(30, interval if not worst else min(interval, 60)))
