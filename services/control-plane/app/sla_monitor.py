"""Independent SLA evaluation, refresh compliance, and in-app incident lifecycle."""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from . import catalog as c, layers
from .models import CatalogAsset, DatasetObservation, MetricDefinition, MetricObservation, Resource, SlaEvaluation, SlaIncident, utcnow


def combine(statuses):
    return next((s for s in ('breached', 'unknown', 'at_risk', 'healthy') if s in statuses), 'not_configured')


def schedule(db, asset, now):
    policy = asset.sla
    zone = ZoneInfo(policy.get('timezone', 'UTC'))
    clock = time.fromisoformat(policy['deadline_time'])
    local = now.astimezone(zone)
    hours = policy.get('window_hours', 168)
    start = c.timestamp(policy.get('starts_at')) or now
    cutoff = max(start, now - timedelta(hours=hours))
    deadlines = []
    for offset in range(-int(hours / 24) - 9, 9):
        day = local.date() + timedelta(days=offset)
        if day.weekday() in policy.get('weekdays', list(range(7))):
            deadlines.append(datetime.combine(day, clock, tzinfo=zone).astimezone(timezone.utc))
    grace = timedelta(minutes=policy.get('grace_minutes', 15))
    observations = list(db.scalars(select(DatasetObservation.observed_at).where(
        DatasetObservation.asset_id == asset.id, DatasetObservation.kind == policy.get('freshness_basis', 'refresh'),
        DatasetObservation.observed_at >= deadlines[0], DatasetObservation.observed_at <= now)))
    # Existing observations can seed current freshness, but never invent a missing
    # period's historical completion when monitoring started later.
    if policy.get('freshness_basis', 'refresh') == 'refresh' and asset.last_success_at:
        observations.append(asset.last_success_at)
    observations = sorted(c.aware(t) for t in observations if c.aware(t) <= now)
    periods = [(before, due) for before, due in zip(deadlines, deadlines[1:]) if due >= start]
    completed = [(before, due) for before, due in periods if due + grace <= now and due >= cutoff]
    def met(before, due):
        return any(before + grace < t <= min(due + grace, now) for t in observations)
    good = sum(met(before, due) for before, due in completed)
    latest = next(((before, due) for before, due in reversed(periods) if due <= now), None)
    upcoming = next(((before, due) for before, due in periods if due > now), None)
    state = 'unknown'
    if latest:
        before, due = latest
        state = 'healthy' if met(before, due) else 'breached' if now >= due + grace else 'at_risk'
    if upcoming:
        before, due = upcoming
        if not met(before, due) and now >= due - timedelta(minutes=policy.get('warning_minutes', 30)) and state != 'breached':
            state = 'at_risk'
    due = latest[1] if latest else upcoming[1] if upcoming else None
    return {'name': 'Scheduled refresh', 'status': state, 'target': policy['deadline_time'] + ' ' + policy.get('timezone', 'UTC'),
            'value': good, 'unit': 'on-time periods', 'samples': len(completed),
            'deadline_at': due.isoformat() if due else None, 'next_deadline_at': upcoming[1].isoformat() if upcoming else None,
            'compliance_percent': round(100 * good / len(completed), 2) if completed else None}


def evaluate(db, asset, resource, now=None):
    now = c.aware(now or utcnow())
    policy = asset.sla or {}
    result = c.base_service_level(db, asset, resource, now)
    checks = list(result['checks'])
    basis = policy.get('freshness_basis', 'refresh')
    if policy.get('freshness_minutes') is not None:
        latest = db.scalar(select(DatasetObservation.observed_at).where(DatasetObservation.asset_id == asset.id,
            DatasetObservation.kind == basis, DatasetObservation.observed_at <= now).order_by(DatasetObservation.observed_at.desc()).limit(1))
        at = max([c.aware(v) for v in [latest, result.get('last_success_at') if basis == 'refresh' else None] if v], default=None)
        age = (now - at).total_seconds() / 60 if at else None
        limit = policy['freshness_minutes']
        state = 'unknown' if age is None else 'breached' if age > limit else 'at_risk' if age >= limit * policy.get('warning_percent', 80) / 100 else 'healthy'
        checks = [item for item in checks if item['name'] != 'Freshness']
        checks.insert(0, {'name': 'Freshness', 'status': state, 'target': limit, 'value': round(age, 1) if age is not None else None, 'unit': 'minutes', 'basis': basis})
        result['last_success_at'] = at
    if policy.get('deadline_time'):
        checks.append(schedule(db, asset, now))
    if policy.get('quality_required'):
        states = []
        for metric in db.scalars(select(MetricDefinition).where(MetricDefinition.asset_id == asset.id,
                MetricDefinition.enabled.is_(True), MetricDefinition.kind == 'quality')):
            observation = db.scalar(select(MetricObservation).where(MetricObservation.metric_id == metric.id)
                .order_by(MetricObservation.observed_at.desc()).limit(1))
            stale = not observation or observation.definition_version != metric.version or c.aware(observation.observed_at) < now - timedelta(minutes=policy.get('quality_max_age_minutes', 1440))
            states.append('unknown' if stale else observation.status)
        checks.append({'name': 'Data quality', 'status': combine(states) if states else 'unknown', 'target': 'passing current checks',
                       'value': sum(state == 'healthy' for state in states), 'samples': len(states), 'unit': 'checks'})
    if not checks:
        return result
    return {**result, 'status': combine({item['status'] for item in checks}), 'checks': checks, 'evaluated_at': now, 'policy': policy}


def record_evaluation(db, asset, resource, now=None):
    now = c.aware(now or utcnow())
    result = jsonable_encoder(evaluate(db, asset, resource, now))
    # Retain changes and daily compliance periods, not a row for every passing tick.
    signature = {**result, 'evaluated_at': None, 'checks': [{k: v for k, v in item.items() if k != 'value' or item['name'] != 'Freshness'} for item in result['checks']]}
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    latest = db.scalar(select(SlaEvaluation).where(SlaEvaluation.asset_id == asset.id).order_by(SlaEvaluation.evaluated_at.desc()).limit(1))
    if not latest or latest.fingerprint != fingerprint:
        db.add(SlaEvaluation(asset_id=asset.id, evaluated_at=now, status=result['status'], fingerprint=fingerprint, result=result))
    incident = db.scalar(select(SlaIncident).where(SlaIncident.asset_id == asset.id, SlaIncident.resolved_at.is_(None)))
    state = result['status']
    if state in ('at_risk', 'breached', 'unknown'):
        if not incident:
            incident = SlaIncident(asset_id=asset.id, status=state, opened_at=now)
            db.add(incident)
        incident.updated_at, incident.status, incident.detail = now, state, result
    elif incident:
        incident.resolved_at, incident.updated_at, incident.status, incident.detail = now, now, 'resolved', result
    db.flush()
    return result


def tick(factory):
    with factory() as db:
        c.harvest_artifacts(db)
        db.commit()
        # The asset row serializes evaluator/acknowledgement/policy changes across
        # replicas. No query, notification delivery, or network request under the lock.
        open_incidents = set(db.scalars(select(SlaIncident.asset_id).where(SlaIncident.resolved_at.is_(None))))
        identities = [ident for ident, policy in db.execute(select(CatalogAsset.id, CatalogAsset.sla))
                      if policy or ident in open_incidents]
        for ident in identities:
            asset = db.scalar(select(CatalogAsset).where(CatalogAsset.id == ident).with_for_update())
            if asset and (asset.sla or db.scalar(select(SlaIncident.id).where(SlaIncident.asset_id == ident, SlaIncident.resolved_at.is_(None)))):
                resource = db.get(Resource, asset.resource_id)
                if resource and not asset.attributes.get('inactive'):
                    record_evaluation(db, asset, resource)
            db.commit()


async def monitor_loop(factory, interval):
    while True:
        try:
            await run_in_threadpool(tick, factory)
        except Exception:
            logging.getLogger(__name__).exception('SLA evaluation failed; retrying next interval')
        await asyncio.sleep(max(5, interval))


def require_inputs(db, workload, user):
    """Opt-in consumer admission; never block a producer from repairing its data."""
    from . import rbac
    root = db.get(CatalogAsset, c.asset_id(workload.resource_id))
    policy = root.sla if root else {}
    if policy.get('enforcement') != 'block_new_runs':
        return
    owner = db.get(Resource, workload.resource_id)
    failures = []
    for ident in policy.get('required_assets', []):
        asset = db.get(CatalogAsset, ident)
        resource = db.get(Resource, asset.resource_id) if asset else None
        if not resource or layers.resource_layer(db, owner) != layers.resource_layer(db, resource) or not rbac.allowed(db, user, 'read', resource):
            failures.append({'asset_id': ident, 'status': 'unavailable'})
            continue
        result = evaluate(db, asset, resource)
        if result['status'] == 'breached' or (policy.get('block_on_unknown', True) and result['status'] in ('unknown', 'not_configured')):
            failures.append({'asset_id': ident, 'name': asset.name, 'status': result['status']})
    if failures:
        raise HTTPException(409, {'message': 'Required upstream data does not meet its SLA. Open the catalog object’s SLA tab for details.',
            'code': 'SLA_INPUT_GATE', 'assets': failures})
