"""Durable metric definitions and immutable, provenance-aware observations."""
import hashlib
import json
import math

from fastapi import HTTPException
from sqlalchemy import select, delete

from . import catalog as c, layers, rbac
from .models import CatalogAsset, DatasetObservation, LineageEdge, MetricDefinition, MetricDefinitionRevision, MetricObservation, Resource, utcnow


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]


def insert_once(db, model, values):
    if db.bind.dialect.name in ('postgresql', 'sqlite'):
        if db.bind.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=['id']))
    elif not db.get(model, values['id']):
        db.add(model(**values))
    db.flush()
    return db.get(model, values['id'])


def dataset_observation(db, asset, at, origin, key, kind='refresh', run_id='', revision=''):
    if not at or c.aware(at) > utcnow():
        return None
    return insert_once(db, DatasetObservation, dict(id=digest(asset.id, kind, origin, key), asset_id=asset.id,
        kind=kind, observed_at=at, origin=origin, run_id=run_id, revision=revision[:128]))


def status(value, definition):
    if value is None or not math.isfinite(value):
        return 'unknown'
    for label, low, high in [('breached', 'minimum', 'maximum'), ('at_risk', 'warn_minimum', 'warn_maximum')]:
        if ((definition.get(low) is not None and value < definition[low]) or
                (definition.get(high) is not None and value > definition[high])):
            return label
    return 'healthy'


def can_attest(db, resource, user=None, access_snapshot=None):
    """Lineage metadata alone cannot authorize statements about another dataset."""
    if resource.kind != 'schema':
        return True
    if access_snapshot is not None:
        return access_snapshot.get(resource.object_id, {}).get(resource.name) in ('Contributor', 'Admin', 'Owner')
    return bool(user and any(rbac.allowed(db, user, action, resource) for action in ('write', 'ddl')))


def define(db, asset, key, name, kind, description='', unit='', definition=None, origin='manual', created_by=''):
    definition = definition or {}
    ident = digest(asset.id, key)
    row = insert_once(db, MetricDefinition, dict(id=ident, asset_id=asset.id, key=key, name=name,
        kind=kind, description=description, unit=unit, definition=definition, origin=origin, created_by=created_by))
    row = db.scalar(select(MetricDefinition).where(MetricDefinition.id == ident).with_for_update().execution_options(populate_existing=True))
    changed = any(getattr(row, k) != v for k, v in dict(name=name, kind=kind, description=description, unit=unit, definition=definition).items())
    if changed:
        row.version += 1
        row.updated_at = utcnow()
        row.name, row.kind, row.description, row.unit, row.definition = name, kind, description, unit, definition
    insert_once(db, MetricDefinitionRevision, {'id':digest(row.id,row.version),'metric_id':row.id,'version':row.version,
        'changed_by':created_by,'definition':{'name':name,'kind':kind,'description':description,'unit':unit,'definition':definition}})
    resource = db.get(Resource, asset.resource_id)
    node = c.upsert(db, resource, 'metric:' + row.id, kind='kpi' if kind == 'kpi' else 'quality_metric', name=name,
        path=asset.path + ' / ' + name, attributes={'description': description, 'metric_id': row.id,
            'parent_asset_id': asset.id, 'dependency_asset_ids': definition.get('dependencies', [asset.id]), 'unit': unit, 'inactive': False})
    db.execute(delete(LineageEdge).where(LineageEdge.target_id == node.id, LineageEdge.origin == 'metric_definition'))
    for dependency in definition.get('dependencies', [asset.id]):
        target = db.get(CatalogAsset, dependency)
        if target:
            c.edge(db, resource, target, node, 'measures', 'metric_definition', str(row.version))
    return row


def observe(db, metric, value, at=None, origin='reported', key=None, run_id='', revision='', detail=None, result_status=None):
    from .models import new_id
    if value is not None and not math.isfinite(value):
        raise HTTPException(422, 'Measurement must be finite')
    details = {**(detail or {}), 'unit':metric.unit, 'definition_fingerprint':digest(metric.definition),
               'thresholds': {k: metric.definition.get(k) for k in ('minimum', 'maximum', 'warn_minimum', 'warn_maximum')}}
    return insert_once(db, MetricObservation, dict(id=digest(metric.id, origin, key) if key else new_id(), metric_id=metric.id,
        observed_at=at or utcnow(), value=value, status=result_status or status(value, metric.definition),
        definition_version=metric.version, origin=origin, run_id=run_id, revision=revision[:128], detail=details))


def dependencies_visible(db, user, asset_ids, browsing=False):
    if not asset_ids:
        return True
    _, allowed = c.readable_resources(db, user, browsing=browsing)
    dependencies = dict(db.execute(select(CatalogAsset.id, CatalogAsset.resource_id).where(
        CatalogAsset.id.in_(asset_ids))).all())
    return all(dependencies.get(ident) in allowed for ident in asset_ids)


def validate_dependencies(db, user, asset, identities):
    resource = db.get(Resource, asset.resource_id)
    ident_set = list(dict.fromkeys([asset.id, *identities]))
    for ident in ident_set:
        other, parent = c.require_asset(db, ident, user)
        if layers.resource_layer(db, parent) != layers.resource_layer(db, resource):
            raise HTTPException(422, 'Metric and dependency must belong to the same layer')
    return ident_set


def observation_out(row):
    return {k: getattr(row, k) for k in ('id', 'observed_at', 'value', 'status', 'definition_version', 'origin', 'run_id', 'revision', 'detail')}


def metric_out(db, row):
    latest = db.scalar(select(MetricObservation).where(MetricObservation.metric_id == row.id)
                       .order_by(MetricObservation.observed_at.desc(), MetricObservation.id.desc()).limit(1))
    return {k: getattr(row, k) for k in ('id', 'asset_id', 'key', 'name', 'kind', 'description', 'unit', 'origin', 'definition', 'version', 'enabled', 'updated_at')} | {
        'latest': observation_out(latest) if latest else None}


def dbt_results(db, logical, physical, definitions, results, revision='', run_id='', producer_id='', program_name='', observation_user=None, access_snapshot=None):
    """Import assertions as numeric failing-row measurements, never as data refreshes."""
    metadata = (results or {}).get('metadata', {})
    active_uids = {uid for uid,node in definitions.items() if node.get('resource_type')=='test'}
    for previous in db.scalars(select(MetricDefinition).where(MetricDefinition.origin=='dbt')):
        cfg=previous.definition
        if cfg.get('producer_id')==producer_id and cfg.get('program_name')==program_name and cfg.get('dbt_unique_id') not in active_uids:
            previous.enabled=False
            target=db.get(CatalogAsset,previous.asset_id)
            retired=db.get(CatalogAsset,c.asset_id(target.resource_id,'metric:'+previous.id)) if target else None
            if retired:
                retired.attributes={**retired.attributes,'inactive':True}
    for result in (results or {}).get('results', []):
        uid = result.get('unique_id')
        node = definitions.get(uid, {})
        if node.get('resource_type') != 'test':
            continue
        dependencies = list(dict.fromkeys(physical[d].id for d in node.get('depends_on', {}).get('nodes', []) if d in physical))
        # Ephemeral models may sit between a test and the physical dataset.
        pending = list(node.get('depends_on', {}).get('nodes', [])); seen = set()
        while pending:
            dep = pending.pop()
            if dep in seen:
                continue
            seen.add(dep)
            if dep in physical and physical[dep].id not in dependencies:
                dependencies.append(physical[dep].id)
            pending.extend(definitions.get(dep, {}).get('depends_on', {}).get('nodes', []))
        targets = [db.get(CatalogAsset, ident) for ident in dependencies]
        targets = [target for target in targets if can_attest(db, db.get(Resource, target.resource_id), observation_user, access_snapshot)]
        targets = targets or ([logical[uid]] if uid in logical else [])
        completed = [c.timestamp(t.get('completed_at')) for t in result.get('timing', []) if t.get('name') == 'execute']
        at = max([v for v in completed if v], default=c.timestamp(metadata.get('generated_at')) or utcnow())
        if at > utcnow():
            continue
        raw = result.get('failures')
        value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) else 0.0 if result.get('status') == 'pass' else None
        state = {'pass': 'healthy', 'warn': 'at_risk', 'fail': 'breached'}.get(result.get('status'), 'unknown')
        for target in targets:
            config = {'maximum': 0, 'dependencies': dependencies or [target.id], 'dbt_unique_id': uid,
                      'severity': node.get('config', {}).get('severity', 'error'), 'producer_id':producer_id, 'program_name':program_name,
                      'warn_if': node.get('config', {}).get('warn_if'), 'error_if': node.get('config', {}).get('error_if')}
            metric = define(db, target, 'dbt:' + digest(producer_id,program_name,uid), str(node.get('name', uid))[:256], 'quality',
                str(node.get('description', ''))[:10000], 'failing rows', config, origin='dbt')
            metric.enabled=True
            # Older SDKs omitted failing-row counts. Let the independent raw-artifact
            # backfill supply these values rather than persisting an incomplete sample.
            if raw is None and result.get('status') in ('warn', 'fail'):
                continue
            execution_at=max([v for v in completed if v],default=None)
            evidence=(execution_at.isoformat() if execution_at else metadata.get('invocation_id') or metadata.get('generated_at') or digest(result.get('status'),value))
            observe(db, metric, value, at, origin='dbt', key=digest(uid,evidence,result.get('status')),
                    run_id=run_id, revision=revision, detail={'dbt_status': result.get('status'), 'execution_seconds': result.get('execution_time')}, result_status=state)
