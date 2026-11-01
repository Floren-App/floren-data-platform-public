"""Alerts on what happens across the platform: failed runs and deploys, data SLAs that fail or recover, access
requests, certificates about to expire. The notification loop scans for what is new and posts each event once to
the channels its rules choose (see notify.channels_for); compliance events are sent where they happen."""
from datetime import timedelta
from hashlib import sha1

from sqlalchemy import select

from . import layers, notify
from .models import (AccessRequest, AppGroup, CatalogAsset, CatalogProduct, CertificateCheck, Resource, Run, SlaIncident, Workload,
                     utcnow)
from .ownership import GLOBAL_ADMIN

WINDOW = timedelta(hours=24)   # how far back a new channel or rule can still be told about an event
LATEST = 500                   # rows looked at per scan; older ones were sent in earlier scans


def _layer(db, resource_id):
    resource = db.get(Resource, resource_id) if resource_id else None
    return layers.info(db, resource)["layer_id"] if resource is not None else None


def _owners(db, resource_id) -> set:
    return notify.people(db, notify.owner_groups(db, resource_id)) if resource_id else set()


def _aware(value):
    from .compliance import _aware as aware
    return aware(value)


def runs(db, since) -> None:
    for run in db.scalars(select(Run).where(Run.status == "failed", Run.workload_id.is_not(None), Run.finished_at.is_not(None))
                          .order_by(Run.finished_at.desc()).limit(LATEST)):
        if _aware(run.finished_at) < since:
            continue
        workload = db.get(Workload, run.workload_id)
        if workload is None:
            continue
        notify.queue(db, "run.failed", _owners(db, workload.resource_id), f"Run failed: {workload.title}",
                     f"A run of {workload.title} failed{f' at commit {run.commit_sha[:7]}' if run.commit_sha else ''}"
                     f"{f': {run.waiting_reason}' if run.waiting_reason else ''}. Its log is on the workload's Logs tab.",
                     key=f"run-failed:{run.id}", resource_id=workload.resource_id, layer_id=_layer(db, workload.resource_id),
                     occurred_at=_aware(run.finished_at))


def deploys(db, since) -> None:
    for workload in db.scalars(select(Workload).where(Workload.sync_error.is_not(None))):
        if not workload.sync_error:
            continue
        seen = _aware(workload.last_checked_at) or utcnow()
        if seen < since:
            continue
        notify.queue(db, "deploy.failed", _owners(db, workload.resource_id), f"Deploy failed: {workload.title}",
                     f"{workload.title} could not be deployed or synced from Git: {workload.sync_error[:500]}",
                     key=f"deploy:{workload.id}:{sha1(workload.sync_error.encode()).hexdigest()[:12]}", resource_id=workload.resource_id,
                     layer_id=_layer(db, workload.resource_id), occurred_at=seen)


def slas(db, since) -> None:
    for incident in db.scalars(select(SlaIncident).order_by(SlaIncident.updated_at.desc()).limit(LATEST)):
        asset = db.get(CatalogAsset, incident.asset_id)
        if asset is None:
            continue
        level = (asset.governance or {}).get("classification")
        common = dict(resource_id=asset.resource_id, layer_id=_layer(db, asset.resource_id), classification=level)
        if _aware(incident.opened_at) >= since:
            notify.queue(db, "sla.breached", _owners(db, asset.resource_id), f"SLA failed: {asset.name}",
                         f"{asset.name} does not meet its SLA ({incident.status}).", key=f"sla:{incident.id}:open",
                         occurred_at=_aware(incident.opened_at), **common)
        if incident.resolved_at is not None and _aware(incident.resolved_at) >= since:
            notify.queue(db, "sla.recovered", _owners(db, asset.resource_id), f"SLA met again: {asset.name}",
                         f"{asset.name} meets its SLA again.", key=f"sla:{incident.id}:resolved",
                         occurred_at=_aware(incident.resolved_at), **common)


def requests(db, since) -> None:
    for request in db.scalars(select(AccessRequest).order_by(AccessRequest.created_at.desc()).limit(LATEST)):
        product = db.get(CatalogProduct, request.product_id)
        if product is None:
            continue
        group = db.get(AppGroup, request.app_group_id)
        common = dict(resource_id=product.access_resource_id, layer_id=product.layer_id)
        if request.status == "pending" and _aware(request.created_at) >= since:
            notify.queue(db, "access.requested", _owners(db, product.access_resource_id), f"Access requested: {product.title}",
                         f"{request.requester} asks access to {product.title} for {group.name if group else 'a group'}: {request.reason}",
                         key=f"request:{request.id}", occurred_at=_aware(request.created_at), **common)
        if request.status in ("approved", "declined") and request.decided_at is not None and _aware(request.decided_at) >= since:
            who = {request.requester_principal_id} if request.requester_principal_id else set()
            notify.queue(db, "access.request_decided", who, f"Access {request.status}: {product.title}",
                         f"Your request for {product.title} was {request.status}{f' by {request.decided_by}' if request.decided_by else ''}"
                         f"{f': {request.decision_note}' if request.decision_note else ''}.",
                         key=f"request-decided:{request.id}", occurred_at=_aware(request.decided_at), **common)


def certificates(db, now) -> None:
    admins = notify.people(db, [GLOBAL_ADMIN])
    for check in db.scalars(select(CertificateCheck)):
        expiring = check.not_after is not None and _aware(check.not_after) - now < timedelta(days=14)
        if not expiring and check.state in ("ok", "valid", "ready"):
            continue
        when = f"{check.not_after:%Y-%m-%d}" if check.not_after else check.state
        notify.queue(db, "certificate.expiring", admins, f"Certificate needs attention: {check.hostname}",
                     f"The certificate for {check.hostname} ({check.key}) is {check.state}; it is valid until {when}. {check.message}",
                     key=f"cert:{check.key}:{when}", layer_id=check.layer_id, occurred_at=_aware(check.checked_at))


def scan(db) -> None:
    now = utcnow()
    since = now - WINDOW
    for step in (runs, deploys, slas, requests):
        step(db, since)
    certificates(db, now)
    db.flush()
