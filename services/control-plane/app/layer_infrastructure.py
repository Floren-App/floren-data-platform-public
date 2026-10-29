"""Versioned desired-state contract for layer infrastructure; contains no credentials.

The HTTP API queues operations. A separately privileged controller reconciles them;
ordinary control-plane requests never create namespaces or wait for Kubernetes.
"""
from typing import List, Literal, Optional
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .models import LayerOperation, Resource, utcnow

DNS_NAME = r'^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$'
QUANTITY = r'^[1-9][0-9]*(?:Mi|Gi|Ti)$'
CATALOG = r'^[a-z][a-z0-9_]{0,62}$'
BUCKET = r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$'


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SecretKeyRef(Contract):
    """Always local to the object's namespace; values are never accepted here."""
    name: str = Field(min_length=1, max_length=253, pattern=DNS_NAME)
    key: str = Field(min_length=1, max_length=253, pattern=r'^[-._a-zA-Z0-9]+$')


class IssuerRef(Contract):
    name: str = Field(min_length=1, max_length=253, pattern=DNS_NAME)
    kind: Literal['Issuer', 'ClusterIssuer'] = 'Issuer'
    group: Literal['cert-manager.io'] = 'cert-manager.io'


class CertificatePolicy(Contract):
    mode: Literal['disabled', 'existing', 'cert_manager'] = 'disabled'
    secret_name: Optional[str] = Field(default=None, min_length=1, max_length=253, pattern=DNS_NAME)
    issuer: Optional[IssuerRef] = None
    trust_bundle: Optional[SecretKeyRef] = None
    duration_hours: int = Field(default=2160, ge=24, le=8760)
    renew_before_hours: int = Field(default=360, ge=1, le=4380)

    @model_validator(mode='after')
    def certificate_source(self):
        if self.renew_before_hours >= self.duration_hours:
            raise ValueError('certificate renewal must precede expiry')
        if self.mode == 'disabled' and (self.secret_name or self.issuer or self.trust_bundle):
            raise ValueError('disabled TLS cannot have certificate or trust references')
        if self.mode == 'existing' and (not self.secret_name or self.issuer):
            raise ValueError('existing TLS requires a secret_name and no issuer')
        if self.mode == 'cert_manager' and (not self.secret_name or not self.issuer):
            raise ValueError('cert_manager TLS requires a secret_name and issuer')
        return self


# A Trino node needs room for its JVM; smaller warehouses are refused for workers (4 GB+ recommended).
MIN_WORKER_MEMORY_GB = 1.0


class TrinoCoordinator(Contract):
    """Plans and coordinates queries; does no query work itself. Its JVM heap is 70% of memory."""
    cpu: float = Field(default=1, gt=0, le=256, allow_inf_nan=False)
    memory: str = Field(default='2Gi', pattern=QUANTITY)

    @model_validator(mode='after')
    def fits_the_jvm(self):
        number, unit = re.fullmatch(r'([0-9]+)(Mi|Gi|Ti)', self.memory).groups()
        if int(number) * {'Mi': 1, 'Gi': 1024, 'Ti': 1024 ** 2}[unit] < 1024:
            raise ValueError('trino.coordinator.memory must be at least 1Gi (smaller coordinators run out of memory)')
        return self


class TrinoWorkers(Contract):
    """Workers run the queries and are shared by everyone querying the layer. All have the size of one
    warehouse, each takes one of its slots, and the layer scales between min (always running) and max."""
    warehouse: Optional[str] = Field(default=None, min_length=1, max_length=64)
    min: int = Field(default=1, ge=1, le=1000)
    max: int = Field(default=1, ge=1, le=1000)
    # Running plus waiting queries one worker should carry; more adds workers (up to max, if the warehouse has room).
    queries_per_worker: int = Field(default=4, ge=1, le=1000)
    # Load must stay below what the workers carry this long before one worker is removed.
    scale_down_after_seconds: int = Field(default=300, ge=60, le=86400)
    # A removed worker first finishes its running queries; this bounds how long it may take.
    drain_seconds: int = Field(default=600, ge=30, le=86400)

    @model_validator(mode='after')
    def bounds(self):
        if self.max < self.min:
            raise ValueError('trino.workers.max must be at least trino.workers.min')
        return self


class TrinoInfrastructure(Contract):
    image: str = Field(default='trinodb/trino:484', min_length=1, max_length=512)
    coordinator: TrinoCoordinator = Field(default_factory=TrinoCoordinator)
    workers: TrinoWorkers = Field(default_factory=TrinoWorkers)
    # Query tokens are verified against the platform's public JWKS, so no signing key is
    # distributed. The node-to-node secret is generated per layer unless this overrides it.
    internal_secret: Optional[SecretKeyRef] = None
    catalog_secret: Optional[str] = Field(default=None, min_length=1, max_length=253, pattern=DNS_NAME)
    tls: CertificatePolicy = Field(default_factory=CertificatePolicy)

    @model_validator(mode='before')
    @classmethod
    def earlier_shape(cls, value):
        """Specs saved before workers came from warehouses: one size for every node, a fixed worker count."""
        if not isinstance(value, dict) or not ({'cpu', 'memory', 'heap'} & value.keys() or isinstance(value.get('workers'), int)):
            return value
        value = dict(value)
        sized = {key: value.pop(key) for key in ('cpu', 'memory') if key in value}
        value.pop('heap', None)
        if sized:
            value['coordinator'] = {**sized, **(value.get('coordinator') or {})}
        if isinstance(value.get('workers'), int):
            count = max(1, value['workers'])
            value['workers'] = {'min': count, 'max': count}
        return value


class LayerStorage(Contract):
    workspace_storage_class: Optional[str] = Field(default=None, min_length=1, max_length=253, pattern=DNS_NAME)
    workspace_size: str = Field(default='20Gi', pattern=QUANTITY)
    workspace_access_mode: Literal['ReadWriteOnce', 'ReadWriteMany'] = 'ReadWriteMany'
    # Run consoles/outputs, dbt docs and environment caches of this layer's jobs and apps.
    artifact_storage_class: Optional[str] = Field(default=None, min_length=1, max_length=253, pattern=DNS_NAME)
    artifact_size: str = Field(default='50Gi', pattern=QUANTITY)
    artifact_access_mode: Literal['ReadWriteOnce', 'ReadWriteMany'] = 'ReadWriteMany'
    artifact_secret: Optional[SecretKeyRef] = None  # reserved: object-storage artifacts
    # Retention is deliberate: destructive cleanup requires a separate, explicit API.
    retention: Literal['Retain'] = 'Retain'


class LayerQuota(Contract):
    cpu: float = Field(default=32, gt=0, le=10000, allow_inf_nan=False)
    memory: str = Field(default='128Gi', pattern=QUANTITY)
    pods: int = Field(default=100, ge=4, le=100000)
    persistent_volume_claims: int = Field(default=100, ge=1, le=100000)
    storage: str = Field(default='1Ti', pattern=QUANTITY)


class OptionalDatabase(Contract):
    enabled: bool = False
    image: str = Field(default='postgres:16.15-bookworm', min_length=1, max_length=512)
    cpu: float = Field(default=0.5, gt=0, le=256, allow_inf_nan=False)
    memory: str = Field(default='1Gi', pattern=QUANTITY)
    # Keys postgres-password and engine-password. Omitted: the controller generates them once
    # inside the layer namespace; they never leave it.
    credentials_secret: Optional[str] = Field(default=None, min_length=1, max_length=253, pattern=DNS_NAME)
    storage_class: Optional[str] = Field(default=None, min_length=1, max_length=253, pattern=DNS_NAME)
    size: str = Field(default='20Gi', pattern=QUANTITY)
    tls: CertificatePolicy = Field(default_factory=CertificatePolicy)
    # Exposes the database to this layer's Trino as a catalog, logging in as the engine role.
    catalog: Optional[str] = Field(default=None, pattern=CATALOG)

    @model_validator(mode='after')
    def credentials(self):
        if self.catalog and not self.enabled:
            raise ValueError('a PostgreSQL catalog requires bundled PostgreSQL')
        return self


class OptionalObjectStore(Contract):
    enabled: bool = False
    # A tested RustFS version/digest must be supplied rather than tracking latest.
    image: Optional[str] = Field(default=None, min_length=1, max_length=512)
    # Keys access-key and secret-key; generated in the layer namespace when omitted.
    credentials_secret: Optional[str] = Field(default=None, min_length=1, max_length=253, pattern=DNS_NAME)
    storage_class: Optional[str] = Field(default=None, min_length=1, max_length=253, pattern=DNS_NAME)
    size: str = Field(default='50Gi', pattern=QUANTITY)
    cpu: float = Field(default=0.5, gt=0, le=256, allow_inf_nan=False)
    memory: str = Field(default='1Gi', pattern=QUANTITY)
    # Created when missing; never deleted by the platform.
    buckets: List[str] = Field(default_factory=lambda: ['lake'], max_length=20)
    tls: CertificatePolicy = Field(default_factory=CertificatePolicy)

    @model_validator(mode='after')
    def credentials(self):
        if self.enabled and not self.image:
            raise ValueError('bundled RustFS requires a pinned image')
        if any(not re.fullmatch(BUCKET, b) for b in self.buckets) or len(set(self.buckets)) != len(self.buckets):
            raise ValueError('RustFS buckets must be distinct S3 bucket names')
        if self.tls.trust_bundle:
            raise ValueError('RustFS tls.trust_bundle is not used; clients trust it through trino.tls.trust_bundle')
        return self


class LayerIceberg(Contract):
    """Iceberg tables in a layer: metadata in the layer PostgreSQL, data in a layer RustFS bucket."""
    enabled: bool = False
    catalog: Optional[str] = Field(default=None, pattern=CATALOG)
    bucket: str = Field(default='lake', pattern=BUCKET)


class InfrastructureSpec(Contract):
    version: Literal[1] = 1
    enabled: bool = False
    # Retired: compute scaled to zero, namespace/volumes/policies retained, new work refused.
    # Removing retained data is an explicit operator step (delete the namespace).
    retired: bool = False
    trino: TrinoInfrastructure = Field(default_factory=TrinoInfrastructure)
    storage: LayerStorage = Field(default_factory=LayerStorage)
    quota: LayerQuota = Field(default_factory=LayerQuota)
    postgres: OptionalDatabase = Field(default_factory=OptionalDatabase)
    rustfs: OptionalObjectStore = Field(default_factory=OptionalObjectStore)
    iceberg: LayerIceberg = Field(default_factory=LayerIceberg)

    @model_validator(mode='after')
    def managed_requirements(self):
        if self.retired and not self.enabled:
            raise ValueError('only managed infrastructure can be retired')
        if self.iceberg.enabled:
            if not (self.postgres.enabled and self.rustfs.enabled):
                raise ValueError('Iceberg needs the layer PostgreSQL (catalog) and RustFS (warehouse)')
            if not self.iceberg.catalog:
                raise ValueError('Iceberg needs a catalog name')
            if self.iceberg.bucket not in self.rustfs.buckets:
                raise ValueError('the Iceberg bucket must be one of the RustFS buckets')
            if self.iceberg.catalog == self.postgres.catalog:
                raise ValueError('the Iceberg and PostgreSQL catalogs need different names')
        catalogs = [c for c in (self.postgres.catalog, self.iceberg.catalog if self.iceberg.enabled else None) if c]
        if 'system' in catalogs:
            raise ValueError('the system catalog name is reserved')
        if catalogs and self.postgres.tls.mode != 'disabled' and not self.trino.tls.trust_bundle:
            raise ValueError('catalogs on TLS PostgreSQL need trino.tls.trust_bundle with its issuing CA')
        if self.rustfs.enabled and self.rustfs.tls.mode != 'disabled' and not self.trino.tls.trust_bundle:
            raise ValueError('RustFS TLS needs trino.tls.trust_bundle with its issuing CA')
        for image in (self.trino.image, self.postgres.image if self.postgres.enabled else None,
                      self.rustfs.image if self.rustfs.enabled else None):
            if image and (image.endswith(':latest') or (':' not in image.rsplit('/', 1)[-1] and '@sha256:' not in image)):
                raise ValueError('infrastructure images must use an explicit version or digest')
        return self


class InfrastructureChange(Contract):
    expected_generation: int = Field(ge=0)
    spec: InfrastructureSpec


class InfrastructureRetry(Contract):
    expected_generation: int = Field(ge=1)


def namespace_for(layer_id, prefix='fdp-layer'):
    if not re.fullmatch(r'[a-f0-9]{12}', layer_id):
        raise ValueError('layer ID must be a stable resource ID')
    value = prefix + '-' + layer_id
    if len(value) > 63 or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', value):
        raise ValueError('invalid layer namespace prefix')
    return value


def engine_ready(db, layer):
    generation = layer.config.get('infrastructure_generation', 0)
    return bool(generation) and db.scalar(select(LayerOperation.id).where(
        LayerOperation.layer_id == layer.id, LayerOperation.generation == generation,
        LayerOperation.phase.in_(('Ready', 'InfrastructureReady')))) is not None


def operation_out(row):
    return {key: getattr(row, key) for key in ('id', 'layer_id', 'generation', 'phase', 'reason',
            'message', 'namespace', 'created_at', 'updated_at', 'finished_at')}


def state(db, layer):
    generation = layer.config.get('infrastructure_generation', 0)
    operation = db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layer.id,
        LayerOperation.generation == generation))
    spec = InfrastructureSpec.model_validate(layer.config.get('infrastructure', {}))
    workers = spec.trino.workers
    scale = layer.config.get('trino_scale') or {}
    return {'layer_id': layer.id, 'generation': generation, 'spec': spec.model_dump(mode='json'),
            'operation': operation_out(operation) if operation else None,
            # What Trino uses now: workers running on their warehouse, and the load that decided it.
            'trino_workers': {'warehouse': workers.warehouse, 'min': workers.min, 'max': workers.max,
                'running': min(workers.max, max(workers.min, layer.config.get('trino_workers') or workers.min)),
                **{key: scale.get(key) for key in ('running_queries', 'queued_queries', 'checked_at',
                                                   'waiting_for_warehouse', 'error')}}}


def configure(db, layer_id, change, retry=False):
    layer = db.scalar(select(Resource).where(Resource.id == layer_id, Resource.kind == 'layer')
                      .with_for_update().execution_options(populate_existing=True))
    if not layer:
        raise HTTPException(404, 'layer not found')
    generation = layer.config.get('infrastructure_generation', 0)
    if generation != change.expected_generation:
        raise HTTPException(409, 'layer infrastructure changed; refresh its generation before updating')
    desired = change.spec.model_dump(mode='json')
    current = InfrastructureSpec.model_validate(layer.config.get('infrastructure', {})).model_dump(mode='json')
    if change.spec.enabled and not change.spec.retired:
        require_worker_warehouse(db, change.spec, layer.id)
    if desired == current and not retry:
        return state(db, layer)
    if current['enabled'] and not desired['enabled']:
        raise HTTPException(409, 'managed infrastructure cannot be disabled through an update; retire it explicitly')
    current_tls = current['trino']['tls']['mode'] != 'disabled'
    desired_tls = desired['trino']['tls']['mode'] != 'disabled'
    if current['enabled'] and current_tls and not desired_tls:
        raise HTTPException(409, 'managed layer TLS cannot be downgraded to plaintext')
    if current['postgres']['enabled']:
        if not desired['postgres']['enabled']:
            raise HTTPException(409, 'bundled PostgreSQL cannot be disabled through an update; retire storage explicitly')
        if any(current['postgres'][key] != desired['postgres'][key]
               for key in ('image', 'credentials_secret', 'storage_class', 'size')):
            raise HTTPException(409, 'existing PostgreSQL image, credentials reference and volume settings require an explicit migration')
        if current['postgres']['tls']['mode'] != 'disabled' and desired['postgres']['tls']['mode'] == 'disabled':
            raise HTTPException(409, 'bundled PostgreSQL TLS cannot be downgraded to plaintext')
    if current['rustfs']['enabled']:
        if not desired['rustfs']['enabled']:
            raise HTTPException(409, 'bundled RustFS cannot be disabled through an update; retire storage explicitly')
        if any(current['rustfs'][key] != desired['rustfs'][key] for key in ('credentials_secret', 'storage_class', 'size')):
            raise HTTPException(409, 'existing RustFS credentials reference and volume settings require an explicit migration')
        if current['rustfs']['tls']['mode'] != 'disabled' and desired['rustfs']['tls']['mode'] == 'disabled':
            raise HTTPException(409, 'bundled RustFS TLS cannot be downgraded to plaintext')
    if current['enabled'] and current_tls != desired_tls:
        from .models import SqlQuery
        if db.scalar(select(SqlQuery.id).where(SqlQuery.engine_layer == layer.id,
                SqlQuery.state.not_in(('FINISHED', 'FAILED', 'CANCELED')), SqlQuery.expires_at > utcnow()).limit(1)):
            raise HTTPException(409, 'wait for active layer SQL requests before enabling HTTPS')
    if generation:
        previous = db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layer.id,
            LayerOperation.generation == generation))
        if previous and previous.phase not in ('Ready', 'InfrastructureReady', 'Failed', 'Superseded', 'Retired'):
            raise HTTPException(409, 'wait for the current layer infrastructure operation to finish')
        if previous and db.scalar(select(LayerOperation.id).where(LayerOperation.id == previous.id,
                LayerOperation.lease_expires_at > utcnow())):
            raise HTTPException(409, 'layer reconciliation is in progress; retry the configuration update shortly')
        if previous and previous.phase in ('InfrastructureReady', 'Ready', 'Retired'):
            previous.phase, previous.reason = 'Superseded', 'DesiredStateChanged'
            previous.message = 'Replaced by a newer infrastructure generation'
            previous.finished_at = utcnow()
    generation += 1
    layer.config = {**layer.config, 'infrastructure': desired, 'infrastructure_generation': generation}
    db.add(LayerOperation(layer_id=layer.id, generation=generation,
        phase='Pending' if change.spec.enabled else 'Ready', reason='ConfigurationAccepted',
        message='Waiting for layer controller' if change.spec.enabled else 'Unmanaged configuration saved',
        finished_at=None if change.spec.enabled else utcnow()))
    db.flush()
    return state(db, layer)


def require_worker_warehouse(db, spec, layer_id=None):
    from .models import ComputePool
    name = spec.trino.workers.warehouse
    pool = db.scalar(select(ComputePool).where(ComputePool.name == name)) if name else None
    if pool is None:
        raise HTTPException(422, 'choose an existing warehouse for the Trino workers (trino.workers.warehouse)')
    if pool.memory_gb < MIN_WORKER_MEMORY_GB:
        raise HTTPException(422, f'Trino workers need a warehouse with at least {MIN_WORKER_MEMORY_GB} GB memory; '
                                 f'{name} has {pool.memory_gb} GB')
    if pool.dedicated:
        raise HTTPException(422, f'{name} is dedicated to its groups; choose a shared warehouse for Trino workers')
    if pool.workload_kinds:
        raise HTTPException(422, f'{name} is limited to {", ".join(pool.workload_kinds)} work; Trino workers need a warehouse of their own')
    if pool.layer_ids and layer_id not in pool.layer_ids:
        raise HTTPException(422, f'{name} is limited to other layers; choose a warehouse meant for this layer')
    # Trino's warehouse is Trino's alone: nothing else may be placed on it.
    from .capacity import default_warehouse, workspace_settings
    from .models import Workload
    others = [f'workload {w.slug}' for w in db.scalars(select(Workload).where(Workload.status != 'stopped'))
              if name in (w.warehouse, ((w.program_def or {}).get('airflow_settings') or {}).get('worker_warehouse'))]
    if default_warehouse(db) == name:
        others.append('the default warehouse setting')
    if workspace_settings(db)['warehouse'] == name:
        others.append('workspaces')
    if others:
        raise HTTPException(422, f'{name} is used by {", ".join(others[:3])}; Trino workers need a warehouse of their own')


def trino_workers(db):
    """(layer, warehouse name, workers running now) for every managed layer; workers take warehouse slots."""
    out = []
    for layer in db.scalars(select(Resource).where(Resource.kind == 'layer')):
        config = layer.config or {}
        if not config.get('infrastructure', {}).get('enabled'):
            continue
        spec = InfrastructureSpec.model_validate(config['infrastructure'])
        if spec.retired or not spec.trino.workers.warehouse:
            continue
        workers = spec.trino.workers
        out.append((layer, workers.warehouse, min(workers.max, max(workers.min, config.get('trino_workers') or workers.min))))
    return out


def _managed(db, layer_id):
    from .layers import layer_of
    ident = layer_of(db, layer_id)  # an empty binding means the default layer
    layer = db.get(Resource, ident) if ident else None
    return layer if layer and layer.config.get('infrastructure', {}).get('enabled') else None


def require_ready(db, layer_id, execution='job'):
    """Fail closed when opting into managed Kubernetes, without changing legacy layers.

    Workspaces need only the layer's infrastructure. Jobs and apps additionally need
    layer-local artifact storage, which reports a separate, fully Ready phase."""
    layer = _managed(db, layer_id)
    if not layer:
        return
    operation = db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layer.id,
        LayerOperation.generation == layer.config.get('infrastructure_generation')))
    accepted = ('Ready', 'InfrastructureReady') if execution == 'workspace' else ('Ready',)
    if operation and operation.phase == 'Retired':
        raise HTTPException(409, {'code': 'LayerRetired', 'message': 'This layer is retired; reactivate its infrastructure to run work here',
            'layer_id': layer.id, 'operation_id': operation.id})
    if not operation or operation.phase not in accepted:
        raise HTTPException(409, {'code':'LayerNotReady', 'message':'Layer infrastructure is not ready for execution',
            'layer_id': layer.id, 'operation_id': operation.id if operation else None})


def placement(db, layer_id):
    """Where an execution of this layer runs; None keeps the shared legacy namespace."""
    from .config import get_settings
    from .orchestrator.base import Placement
    layer = _managed(db, layer_id)
    if not layer:
        return None
    prefix = get_settings().layer_namespace_prefix
    if not prefix:
        raise HTTPException(503, 'managed layer placement is not configured')
    storage = InfrastructureSpec.model_validate(layer.config['infrastructure']).storage
    return Placement(namespace=namespace_for(layer.id, prefix), storage_class=storage.workspace_storage_class,
                     volume_size=storage.workspace_size, access_mode=storage.workspace_access_mode,
                     trust_configmap='fdp-trust' if get_settings().layer_trust_bundle else None)


def managed_namespaces(db):
    from .config import get_settings
    prefix = get_settings().layer_namespace_prefix
    if not prefix:
        return []
    layers = db.scalars(select(Resource).where(Resource.kind == 'layer'))
    return sorted(namespace_for(layer.id, prefix) for layer in layers
                  if (layer.config or {}).get('infrastructure', {}).get('enabled'))


def retirement_blockers(db, layer_id):
    """Live work that must stop before a layer's compute is scaled to zero."""
    from .layers import resource_layer
    from .models import Workload, Run, Workspace
    from .rbac import workload_resource
    blockers = []
    live = [p for p in db.scalars(select(Workload).where(Workload.status != 'stopped'))
            if resource_layer(db, workload_resource(db, p)) == layer_id]
    if live:
        blockers.append(f"{len(live)} live workload(s), e.g. {live[0].slug}")
    if db.scalar(select(Workspace.id).where(Workspace.environment_id == layer_id, Workspace.status.in_(('starting', 'running')))):
        blockers.append('running workspaces')
    if db.scalar(select(Run.id).where(Run.environment_id == layer_id, Run.status.in_(('pending', 'running')))):
        blockers.append('active runs')
    return blockers
