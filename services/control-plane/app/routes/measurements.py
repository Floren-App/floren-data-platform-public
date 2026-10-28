"""Catalog metrics, governed SQL measurement, history, and in-app SLA alerts."""
import logging
import math
from dataclasses import replace
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .. import catalog as c, layers, measurements as m, rbac, sql_access, sla_monitor
from ..auth import User
from ..db import get_db
from ..deps import get_actor
from ..models import CatalogAsset, DataSource, DatasetObservation, MetricDefinition, MetricDefinitionRevision, MetricObservation, Resource, Run, SlaEvaluation, SlaIncident, utcnow
from .common import audit

router = APIRouter(prefix='/v1/catalog', tags=['metrics and quality'])


class MetricIn(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    key: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_.-]+$')
    name: str = Field(min_length=1, max_length=256)
    kind: Literal['kpi', 'quality'] = 'kpi'
    description: str = Field(default='', max_length=10000)
    unit: str = Field(default='', max_length=64)
    source: str = Field(default='', max_length=64)
    sql: str = Field(default='', max_length=100000)
    dependencies: List[str] = Field(default_factory=list, max_length=100)
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    warn_minimum: Optional[float] = None
    warn_maximum: Optional[float] = None
    enabled: bool = True

    @model_validator(mode='after')
    def valid(self):
        if bool(self.source) != bool(self.sql):
            raise ValueError('source and SQL must be configured together')
        for low, high in ((self.minimum, self.maximum), (self.warn_minimum, self.warn_maximum)):
            if low is not None and high is not None and low > high:
                raise ValueError('minimum must not exceed maximum')
        if self.minimum is not None and self.warn_minimum is not None and self.warn_minimum < self.minimum:
            raise ValueError('warning minimum must be inside the failure range')
        if self.maximum is not None and self.warn_maximum is not None and self.warn_maximum > self.maximum:
            raise ValueError('warning maximum must be inside the failure range')
        return self


class ObservationIn(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    value: Optional[float] = None
    numerator: Optional[float] = Field(default=None, ge=0)
    denominator: Optional[float] = Field(default=None, gt=0)
    observed_at: Optional[datetime] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=256)


class DatasetObservationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['checked', 'watermark', 'refresh']
    observed_at: Optional[datetime] = None
    idempotency_key: str = Field(min_length=1, max_length=256)


def require_metric(db, asset_id, metric_id, user):
    asset, resource = c.require_asset(db, asset_id, user)
    row = db.get(MetricDefinition, metric_id)
    if not row or row.asset_id != asset.id or not m.dependencies_visible(db, user, row.definition.get('dependencies', [])):
        raise HTTPException(404, 'Metric not available')
    return row, asset, resource


def definition_query(sql):
    from sqlglot import exp, parse, errors
    logging.getLogger('sqlglot').setLevel(logging.ERROR)
    try:
        nodes = parse(sql, read='trino')
        if len(nodes) != 1 or not isinstance(nodes[0], exp.Query) or any(isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Command)) for n in nodes[0].walk()):
            raise ValueError()
        sql_access.validate_statement(sql)
    except (errors.SqlglotError, ValueError):
        raise HTTPException(422, 'Metric SQL must be one read-only SELECT query') from None


@router.get('/assets/{ident}/metrics')
def metrics(ident: str, db=Depends(get_db), user: User = Depends(get_actor)):
    asset, _ = c.require_asset(db, ident, user)
    return [m.metric_out(db, row) for row in db.scalars(select(MetricDefinition).where(MetricDefinition.asset_id == asset.id).order_by(MetricDefinition.name))
            if m.dependencies_visible(db, user, row.definition.get('dependencies', []))]


@router.post('/assets/{ident}/metrics', status_code=201)
def define(ident: str, body: MetricIn, db=Depends(get_db), user: User = Depends(get_actor)):
    asset, resource = c.require_asset(db, ident, user, 'assign')
    if asset.kind in ('kpi', 'quality_metric'):
        raise HTTPException(422, 'Attach definitions to the underlying data product')
    dependencies = m.validate_dependencies(db, user, asset, body.dependencies)
    if body.sql:
        definition_query(body.sql)
        source = db.scalar(select(DataSource).where(DataSource.name == body.source))
        if not source or source.kind not in sql_access.SQL_KINDS or source.config.get('system_metadata'):
            raise HTTPException(422, 'Choose a governed SQL data source')
        if layers.layer_of(db, source.config.get('layer_id') or source.config.get('environment_id')) != layers.resource_layer(db, resource):
            raise HTTPException(422, 'Metric SQL source must belong to the same layer')
        if not any(db.get(Resource, db.get(CatalogAsset, dep).resource_id).object_id == source.id for dep in dependencies):
            raise HTTPException(422, 'Declare a dataset dependency in the SQL source')
    row = db.scalar(select(MetricDefinition).where(MetricDefinition.asset_id == ident, MetricDefinition.key == body.key))
    if row and row.origin != 'manual':
        raise HTTPException(409, 'Manage this definition in its source project')
    config = body.model_dump(exclude={'key', 'name', 'kind', 'description', 'unit', 'enabled'})
    config['dependencies'] = dependencies
    row = m.define(db, asset, body.key, body.name, body.kind, body.description, body.unit, config, created_by=user.principal_id)
    row.enabled = body.enabled
    node = db.get(CatalogAsset, c.asset_id(resource.id, 'metric:' + row.id))
    node.attributes = {**node.attributes, 'inactive': not row.enabled}
    audit(db, user.username, 'metric.define', row.id, asset_id=ident, version=row.version, enabled=row.enabled)
    db.commit()
    return m.metric_out(db, row)


@router.get('/assets/{ident}/metrics/{metric_id}/history')
def history(ident: str, metric_id: str, limit: int = Query(default=100, ge=1, le=1000), db=Depends(get_db), user: User = Depends(get_actor)):
    row, _, _ = require_metric(db, ident, metric_id, user)
    return [m.observation_out(o) for o in db.scalars(select(MetricObservation).where(MetricObservation.metric_id == row.id)
        .order_by(MetricObservation.observed_at.desc(), MetricObservation.id.desc()).limit(limit))]


@router.get('/assets/{ident}/metrics/{metric_id}/versions')
def versions(ident: str, metric_id: str, limit: int = Query(default=100, ge=1, le=1000), db=Depends(get_db), user: User = Depends(get_actor)):
    row, _, _ = require_metric(db, ident, metric_id, user)
    return [{'version':v.version,'created_at':v.created_at,'definition':v.definition} for v in db.scalars(
        select(MetricDefinitionRevision).where(MetricDefinitionRevision.metric_id==row.id).order_by(MetricDefinitionRevision.version.desc()).limit(limit))]


def provenance(db, user):
    kind, _, ident = user.execution_context.partition(':')
    run = db.get(Run, ident) if kind == 'run' else None
    return (run.id, run.commit_sha or '') if run else ('', '')


@router.post('/assets/{ident}/metrics/{metric_id}/observations', status_code=201)
def report(ident: str, metric_id: str, body: ObservationIn, db=Depends(get_db), user: User = Depends(get_actor)):
    row, asset, resource = require_metric(db, ident, metric_id, user)
    if not row.enabled or row.origin != 'manual':
        raise HTTPException(409, 'Only enabled manual metrics accept reported values')
    if not any(rbac.allowed(db, user, action, resource) for action in ('write', 'edit')):
        raise HTTPException(403, 'Contributor or higher required to report measurements')
    at = c.aware(body.observed_at or utcnow())
    if at > utcnow():
        raise HTTPException(422, 'Observation time cannot be in the future')
    run_id, revision = provenance(db, user)
    observation = m.observe(db, row, body.value, at, key=body.idempotency_key, run_id=run_id, revision=revision,
        detail={'numerator': body.numerator, 'denominator': body.denominator})
    audit(db, user.username, 'metric.report', row.id, observation=observation.id)
    db.commit()
    return m.observation_out(observation)


@router.post('/assets/{ident}/metrics/{metric_id}/measure')
async def measure(ident: str, metric_id: str, request: Request, db=Depends(get_db), authorization: str = Header(default=''), x_fdp_viewer: str = Header(default='')):
    from .sql import QueryIn, enabled, query_for_actor, transaction_call
    from starlette.concurrency import run_in_threadpool
    enabled()
    who = await run_in_threadpool(transaction_call, db, sql_access.actor, authorization, x_fdp_viewer)
    row, asset, resource = require_metric(db, ident, metric_id, who.user)
    if not row.enabled or not row.definition.get('sql'):
        raise HTTPException(409, 'This metric has no enabled SQL calculation')
    definition_query(row.definition['sql'])
    layer = layers.resource_layer(db, resource)
    if who.user.environment_id is not None and layers.canonical(db, who.user.environment_id) != layer:
        raise HTTPException(403, 'Cross-layer measurement is forbidden')
    limits = {}
    for dep in row.definition['dependencies']:
        dependency = db.get(CatalogAsset, dep)
        schema = db.get(Resource, dependency.resource_id)
        source = db.get(DataSource, schema.object_id) if schema.kind == 'schema' else None
        if source and source.kind in sql_access.SQL_KINDS:
            limits.setdefault(sql_access.catalog_name(source), []).append(schema.name)
    who = replace(who, user=replace(who.user, environment_id=layer), scope_limit=limits)
    version = row.version
    result = await query_for_actor(QueryIn(source=row.definition['source'], sql=row.definition['sql'], max_rows=2), request, db, who)
    if len(result['rows']) != 1 or not result['rows'][0]:
        raise HTTPException(422, 'Metric SQL must return exactly one row; first column is the numeric value')
    try:
        value = None if result['rows'][0][0] is None else float(result['rows'][0][0])
        if value is not None and not math.isfinite(value):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, 'Metric SQL must return a finite numeric value') from None
    db.refresh(row)
    if row.version != version:
        raise HTTPException(409, 'Metric definition changed during evaluation; measure again')
    run_id, revision = provenance(db, who.user)
    observation = m.observe(db, row, value, origin='trino', key=result['query_id'], run_id=run_id, revision=revision,
        detail={'query_id': result['query_id']})
    audit(db, who.user.username, 'metric.measure', row.id, query=result['query_id'])
    db.commit()
    return m.observation_out(observation)


@router.post('/assets/{ident}/observations', status_code=201)
def dataset_report(ident: str, body: DatasetObservationIn, db=Depends(get_db), user: User = Depends(get_actor)):
    asset, resource = c.require_asset(db, ident, user)
    if not any(rbac.allowed(db, user, action, resource) for action in ('write', 'edit')):
        raise HTTPException(403, 'Contributor or higher required to report a dataset observation')
    at = c.aware(body.observed_at or utcnow())
    if at > utcnow():
        raise HTTPException(422, 'Observation time cannot be in the future')
    run_id, revision = provenance(db, user)
    observation = m.dataset_observation(db, asset, at, 'reported', body.idempotency_key, body.kind, run_id, revision)
    if body.kind == 'refresh' and (not asset.last_success_at or c.aware(asset.last_success_at) < at):
        asset.last_success_at = at
    audit(db, user.username, 'dataset.observation', ident, kind=body.kind, observation=observation.id)
    db.commit()
    return {'id': observation.id, 'kind': observation.kind, 'observed_at': observation.observed_at}


@router.get('/assets/{ident}/sla-history')
def sla_history(ident: str, limit: int = Query(default=100, ge=1, le=1000), db=Depends(get_db), user: User = Depends(get_actor)):
    c.require_asset(db, ident, user)
    return [{'id': row.id, 'evaluated_at': row.evaluated_at, 'status': row.status, 'result': row.result}
            for row in db.scalars(select(SlaEvaluation).where(SlaEvaluation.asset_id == ident).order_by(SlaEvaluation.evaluated_at.desc()).limit(limit))]


@router.post('/assets/{ident}/evaluate-sla')
def evaluate_sla(ident: str, db=Depends(get_db), user: User = Depends(get_actor)):
    asset, resource = c.require_asset(db, ident, user)
    db.scalar(select(CatalogAsset).where(CatalogAsset.id == ident).with_for_update())
    result = sla_monitor.record_evaluation(db, asset, resource)
    db.commit()
    return result


@router.get('/alerts')
def alerts(asset_id: str = '', include_resolved: bool = False, limit: int = Query(default=100, ge=1, le=500), db=Depends(get_db), user: User = Depends(get_actor)):
    incidents = select(SlaIncident.asset_id)
    if asset_id:
        incidents = incidents.where(SlaIncident.asset_id == asset_id)
    if not include_resolved:
        incidents = incidents.where(SlaIncident.resolved_at.is_(None))
    # Do not enumerate the entire catalog just to display a handful of alerts.
    visible, _ = c.visible_assets(db, user, condition=CatalogAsset.id.in_(incidents))
    ids = {asset.id for asset in visible}
    names = {asset.id:asset.name for asset in visible}
    statement = select(SlaIncident).where(SlaIncident.asset_id.in_(ids))
    if asset_id:
        statement = statement.where(SlaIncident.asset_id == asset_id)
    if not include_resolved:
        statement = statement.where(SlaIncident.resolved_at.is_(None))
    return [{**{k: getattr(row, k) for k in ('id', 'asset_id', 'status', 'opened_at', 'updated_at', 'resolved_at', 'acknowledged_at', 'acknowledged_by', 'detail')},'asset_name':names[row.asset_id]}
            for row in db.scalars(statement.order_by(SlaIncident.updated_at.desc()).limit(limit))]


@router.post('/alerts/{incident_id}/acknowledge')
def acknowledge(incident_id: str, db=Depends(get_db), user: User = Depends(get_actor)):
    incident = db.get(SlaIncident, incident_id)
    if not incident:
        raise HTTPException(404, 'Alert not found')
    asset, resource = c.require_asset(db, incident.asset_id, user, 'assign')
    db.scalar(select(CatalogAsset).where(CatalogAsset.id == asset.id).with_for_update())
    incident.acknowledged_at, incident.acknowledged_by = utcnow(), user.principal_id
    audit(db, user.username, 'sla.acknowledge', incident.id, asset_id=asset.id)
    db.commit()
    return {'id': incident.id, 'acknowledged_at': incident.acknowledged_at}
