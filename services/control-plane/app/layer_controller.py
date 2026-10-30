"""Opt-in, separately privileged Kubernetes layer reconciler.

Run with ``python -m app.layer_controller``. Never loads a developer kubeconfig.
No namespace/PVC deletion, Secret reads, or credential copying is performed. The only
Secret it writes is each layer's generated Trino node secret, created once and never read.
"""
from copy import deepcopy
from typing import Optional
from dataclasses import dataclass
from datetime import timedelta
import logging
import json
import os
import secrets
from pathlib import Path
import signal
import socket
import ssl
import struct
import time
import uuid

from kubernetes.client.exceptions import ApiException
import httpx
from sqlalchemy import and_, or_, select, update

from .db import SessionLocal
from .layer_infrastructure import InfrastructureSpec
from .layer_manifests import (ControllerConfig, INTERNAL_SECRET, LAYER, OWNER, COMPONENT, ProvisioningError, WorkerPlan,
                              generated_credentials, manifests, worker_plan)
from .models import LayerOperation, Resource, utcnow

LEASE_SECONDS = 90
ACTIVE_PHASES = ('Pending', 'Provisioning', 'InfrastructureReady')
READY_FILE = Path('/tmp/fdp-layer-controller-ready')
RESOURCES = {
    'Kafka': ('/apis/kafka.strimzi.io/v1', 'kafkas', True),
    'KafkaNodePool': ('/apis/kafka.strimzi.io/v1', 'kafkanodepools', True),
    'KafkaTopic': ('/apis/kafka.strimzi.io/v1', 'kafkatopics', True),
    'Namespace': ('/api/v1', 'namespaces', False),
    'NetworkPolicy': ('/apis/networking.k8s.io/v1', 'networkpolicies', True),
    'ResourceQuota': ('/api/v1', 'resourcequotas', True),
    'LimitRange': ('/api/v1', 'limitranges', True),
    'ServiceAccount': ('/api/v1', 'serviceaccounts', True),
    'RoleBinding': ('/apis/rbac.authorization.k8s.io/v1', 'rolebindings', True),
    'ConfigMap': ('/api/v1', 'configmaps', True),
    'PersistentVolumeClaim': ('/api/v1', 'persistentvolumeclaims', True),
    'Deployment': ('/apis/apps/v1', 'deployments', True),
    'Service': ('/api/v1', 'services', True),
    'Certificate': ('/apis/cert-manager.io/v1', 'certificates', True),
    'CiliumNetworkPolicy': ('/apis/cilium.io/v2', 'ciliumnetworkpolicies', True),
}


class LostLease(Exception):
    pass


SERVICE_ACCOUNT = Path('/var/run/secrets/kubernetes.io/serviceaccount')


class InClusterApi:
    """Plain HTTPS to the API server. Avoids the generated client's private call_api,
    whose signature changes between kubernetes-client releases."""
    def __init__(self, host=None, token_file=SERVICE_ACCOUNT / 'token', ca_file=SERVICE_ACCOUNT / 'ca.crt'):
        if host is None:
            host = 'https://%s:%s' % (os.environ['KUBERNETES_SERVICE_HOST'], os.environ['KUBERNETES_SERVICE_PORT'])
        self.token_file = Path(token_file)
        self.http = httpx.Client(base_url=host, verify=str(ca_file) if host.startswith('https') else False,
                                 timeout=httpx.Timeout(15, connect=5), follow_redirects=False)

    def call_api(self, path, method, body=None, query_params=(), header_params=None):
        # Bound tokens rotate; read the current one for every request.
        headers = {**(header_params or {}), 'Authorization': 'Bearer ' + self.token_file.read_text().strip()}
        response = self.http.request(method, path, params=list(query_params),
                                     content=json.dumps(body) if body is not None else None, headers=headers)
        if response.status_code >= 400:
            raise ApiException(status=response.status_code, reason=response.reason_phrase)
        return response.json()


class KubernetesResources:
    """Small API boundary; ownership checks plus resourceVersion protect adoption."""
    def __init__(self, api=None):
        self.api = api or InClusterApi()

    def request(self, method, path, body=None, apply=False):
        return self.api.call_api(path, method, body=body,
            query_params=([('fieldManager', 'fdp-layer-controller'), ('force', 'true')] if apply else
                          [('fieldManager', 'fdp-layer-controller')] if method == 'POST' else []),
            header_params={'Accept': 'application/json',
                'Content-Type': 'application/apply-patch+yaml' if apply else 'application/json'})

    def exists(self, kind, name, namespace=None):
        root, plural, namespaced = RESOURCES[kind]
        path = root + ('/namespaces/' + namespace if namespaced else '') + '/' + plural + '/' + name
        try:
            self.request('GET', path)
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

    def apply(self, document):
        root, plural, namespaced = RESOURCES[document['kind']]
        metadata = document['metadata']
        collection = root + ('/namespaces/' + metadata['namespace'] if namespaced else '') + '/' + plural
        path = collection + '/' + metadata['name']
        try:
            existing = self.request('GET', path)
        except ApiException as exc:
            if exc.status != 404:
                raise
            # CREATE never overwrites an object that appeared after the GET.
            try:
                return self.request('POST', collection, document)
            except ApiException as create_error:
                if document['kind'] in ('Kafka', 'KafkaNodePool', 'KafkaTopic') and create_error.status == 404:
                    raise ProvisioningError('StrimziUnavailable', 'Install the supported Strimzi operator and v1 CRDs before enabling Kafka') from None
                if document['kind'] == 'Certificate' and create_error.status == 404:
                    raise ProvisioningError('CertificateManagerUnavailable', 'Install cert-manager and its Certificate CRD before requesting certificate issuance') from None
                raise
        labels = existing.get('metadata', {}).get('labels', {})
        if any(labels.get(key) != metadata['labels'][key] for key in (OWNER, LAYER)):
            raise ProvisioningError('OwnershipConflict', 'A Kubernetes resource is not owned by this installation and layer; automatic adoption is refused')
        if existing['metadata'].get('deletionTimestamp'):
            raise ProvisioningError('ResourceTerminating', 'A managed Kubernetes resource is terminating; restore or retire the layer explicitly')
        # The object is verified as ours and the write is pinned to the version just read,
        # so forcing only reclaims fields from our own create and from manual edits (drift).
        patch = deepcopy(document)
        patch['metadata']['resourceVersion'] = existing['metadata']['resourceVersion']
        return self.request('PATCH', path, patch, apply=True)

    def create_secret_once(self, namespace, name, labels, data):
        """Create-only: an existing Secret is kept as is, so values never need to be read back."""
        body = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                'metadata': {'name': name, 'namespace': namespace, 'labels': labels}, 'stringData': data}
        try:
            self.request('POST', '/api/v1/namespaces/%s/secrets' % namespace, body)
        except ApiException as exc:
            if exc.status != 409:
                raise

    def source_policies(self, namespace, installation, layer_id):
        from .layer_egress import SOURCE_EGRESS
        selector = '%s=%s,%s=%s,%s=true' % (OWNER, installation, LAYER, layer_id, SOURCE_EGRESS)
        path = '/apis/networking.k8s.io/v1/namespaces/%s/networkpolicies' % namespace
        result, continuation, seen = [], '', set()
        while True:
            page = self.api.call_api(path, 'GET', query_params=[
                ('labelSelector', selector), ('limit', '1000'), ('continue', continuation)],
                header_params={'Accept': 'application/json'})
            result.extend(page['items'])
            continuation = page.get('metadata', {}).get('continue', '')
            if not continuation:
                return result
            if continuation in seen or len(result) >= 100000:
                raise ProvisioningError('IncompletePolicyInventory', 'Kubernetes returned an incomplete data-source policy inventory')
            seen.add(continuation)

    def verify_https(self, hostname):
        try:
            # Public CA bundle comes from the platform mount; private keys stay
            # in the layer. A fresh client also picks up rotated trust roots.
            with httpx.Client(timeout=5, follow_redirects=False) as client:
                response = client.get('https://%s:8443/v1/info' % hostname)
                return response.status_code == 200 and response.json().get('starting') is False
        except (httpx.HTTPError, OSError, ValueError):
            return False

    def served_certificate(self, hostname, port, postgres=False):
        from .cert_monitor import served_certificate
        return served_certificate(hostname, port, postgres=postgres)

    def verify_postgres(self, hostname):
        """Verify the native PostgreSQL TLS handshake without reading a DB password."""
        try:
            context = ssl.create_default_context()
            with socket.create_connection((hostname, 5432), timeout=5) as transport:
                transport.sendall(struct.pack('!II', 8, 80877103))  # PostgreSQL SSLRequest.
                if transport.recv(1) != b'S':
                    return False
                with context.wrap_socket(transport, server_hostname=hostname):
                    return True
        except (OSError, ValueError):
            return False

    def verify_kafka(self, hostname):
        """Native TLS handshake only; no broker password or topic access needed."""
        try:
            context = ssl.create_default_context()
            with socket.create_connection((hostname, 9093), timeout=5) as transport:
                with context.wrap_socket(transport, server_hostname=hostname):
                    return True
        except (OSError, ValueError):
            return False


@dataclass(frozen=True)
class Claim:
    operation_id: str
    layer_id: str
    generation: int
    token: str
    spec: InfrastructureSpec
    # This generation was already Ready: Trino and storage were proven, so a later rollout of the same
    # generation (a platform upgrade rolling helper pods, a rescheduled pod) keeps work admitted.
    ready: bool = False
    # Digest of the external source catalogs the control plane wrote into the layer ('' for none).
    source_catalogs: str = ''
    # Trino workers: their warehouse's size now, and how many run (None: the warehouse is missing).
    workers: Optional[WorkerPlan] = None
    # Whether the layer allows workspaces (their editors start as root): sets the namespace's Pod Security level.
    workspaces: bool = True
    kafka_workers: Optional[WorkerPlan] = None


def claim_operation(session_factory=SessionLocal, now=None):
    now = now or utcnow()
    # Steady-state managed layers stay claimable so drift is repaired; unmanaged "Ready" saves are not.
    available = and_(or_(LayerOperation.phase.in_(ACTIVE_PHASES),
                         and_(LayerOperation.phase == 'Ready', LayerOperation.reason.in_(('Reconciled', 'RollingUpdate'))),
                         # Retired layers are watched until an operator removes the retained storage.
                         and_(LayerOperation.phase == 'Retired', LayerOperation.reason.in_(('StorageRetained', 'StorageDeleting')))),
        or_(LayerOperation.lease_expires_at.is_(None), LayerOperation.lease_expires_at <= now),
        or_(LayerOperation.next_attempt_at.is_(None), LayerOperation.next_attempt_at <= now))
    with session_factory() as db:
        candidates = db.scalars(select(LayerOperation.id).where(available)
                                .order_by(LayerOperation.updated_at, LayerOperation.id).limit(100)).all()
        for operation_id in candidates:
            operation = db.get(LayerOperation, operation_id)
            # Same lock order as the desired-state API. No Kubernetes calls under this lock.
            layer = db.scalar(select(Resource).where(Resource.id == operation.layer_id)
                              .with_for_update().execution_options(populate_existing=True))
            if not layer or layer.config.get('infrastructure_generation') != operation.generation:
                db.rollback()
                continue
            token = uuid.uuid4().hex
            count = db.execute(update(LayerOperation).where(LayerOperation.id == operation_id, available)
                .values(lease_owner=token, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
                .execution_options(synchronize_session=False))
            if count.rowcount != 1:
                db.rollback()
                continue
            spec = InfrastructureSpec.model_validate(layer.config.get('infrastructure', {}))
            from .layer_kafka import broker_plan
            result = Claim(operation_id, layer.id, operation.generation, token, spec, ready=operation.phase == 'Ready',
                           source_catalogs=layer.config.get('source_catalogs', ''),
                           workers=worker_plan(db, spec, layer.config.get('trino_workers')),
                           workspaces=bool(layer.config.get('workspaces', True)),
                           kafka_workers=broker_plan(db, spec, layer.id))
            # A periodic drift check keeps a ready layer admitting work while it runs.
            if operation.phase not in ('InfrastructureReady', 'Ready', 'Retired'):
                operation.phase, operation.reason = 'Provisioning', 'Reconciling'
                operation.message = 'Applying managed layer infrastructure'
            db.commit()
            return result
    return None


def renew(claim, session_factory=SessionLocal, namespace=None):
    now = utcnow()
    with session_factory() as db:
        values = {'lease_expires_at': now + timedelta(seconds=LEASE_SECONDS)}
        if namespace:
            values['namespace'] = namespace
        changed = db.execute(update(LayerOperation).where(LayerOperation.id == claim.operation_id,
            LayerOperation.lease_owner == claim.token, LayerOperation.lease_expires_at > now)
            .values(**values)
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback()
            raise LostLease()
        db.commit()


def finish(claim, phase, reason, message, delay, session_factory=SessionLocal):
    now = utcnow()
    with session_factory() as db:
        changed = db.execute(update(LayerOperation).where(LayerOperation.id == claim.operation_id,
            LayerOperation.lease_owner == claim.token, LayerOperation.lease_expires_at > now)
            .values(phase=phase, reason=reason, message=message,
                finished_at=now if phase in ('Failed', 'InfrastructureReady', 'Ready', 'Retired') else None,
                next_attempt_at=now + timedelta(seconds=delay), lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback()
            raise LostLease()
        db.commit()


def monitor_certificates(claim, resources, namespace, issued, session_factory=SessionLocal):
    """Record the certificates this layer serves; monitoring never blocks reconciliation."""
    from . import cert_monitor
    endpoints = [('trino', 8443, False, claim.spec.trino.tls.mode != 'disabled'),
                 ('postgres', 5432, True, claim.spec.postgres.enabled and claim.spec.postgres.tls.mode != 'disabled'),
                 ('rustfs', 9000, False, claim.spec.rustfs.enabled and claim.spec.rustfs.tls.mode != 'disabled'),
                 ('kafka-kafka-bootstrap', 9093, False, claim.spec.kafka.enabled)]
    try:
        with session_factory() as db:
            keys = []
            for component, port, postgres, enabled in endpoints:
                if not enabled:
                    continue
                hostname = 'fdp-%s.%s.svc' % (component, namespace)
                key = 'layer:%s:%s' % (claim.layer_id, component)
                cert_monitor.record(db, key, 'layer', hostname, layer_id=claim.layer_id,
                                    served=resources.served_certificate(hostname, port, postgres=postgres),
                                    status=issued.get('fdp-' + component))
                keys.append(key)
            cert_monitor.forget(db, claim.layer_id, keep=keys)
            db.commit()
    except Exception:
        logging.getLogger(__name__).exception('certificate monitoring failed for layer %s', claim.layer_id)


def deployment_ready(resource):
    spec, status = resource['spec'], resource.get('status', {})
    desired = spec.get('replicas', 1)
    return (status.get('observedGeneration', 0) >= resource['metadata'].get('generation', 1)
        and status.get('updatedReplicas', 0) == desired
        and status.get('readyReplicas', 0) == desired
        and status.get('replicas', 0) == desired)


def reconcile_egress(claim, resources, config, session_factory):
    from . import layer_egress as egress
    from .layer_infrastructure import namespace_for
    with session_factory() as db:
        sources = egress.inventory(db, claim.layer_id)
        desired, problems = egress.plan(sources, claim.layer_id, config,
            egress.NetworkBoundary(config.pod_cidrs, config.service_cidrs, config.node_cidrs, config.protected_cidrs))
    namespace = namespace_for(claim.layer_id, config.namespace_prefix)
    renew(claim, session_factory)
    existing = resources.source_policies(namespace, config.installation, claim.layer_id)
    names = {doc['metadata']['name'] for doc in desired}
    # Clear removed grants before adding new ones. Policies are additive: adding
    # another default-deny does not revoke a stale allow policy.
    stale = [egress.disabled(doc) for doc in existing if doc['metadata']['name'] not in names]
    for document in [*stale, *desired]:
        renew(claim, session_factory)
        resources.apply(document)
    return problems


def _retired_workers(spec):
    """Retiring scales workers to zero, so it must not depend on their warehouse still existing."""
    from .layer_infrastructure import MIN_WORKER_MEMORY_GB
    return WorkerPlan(spec.trino.workers.warehouse or 'retired', 0.1, MIN_WORKER_MEMORY_GB, 0)


def retire(claim, resources, config, session_factory):
    """Scale every layer deployment to zero; never recreate storage an operator deleted."""
    from .layer_infrastructure import namespace_for
    namespace = namespace_for(claim.layer_id, config.namespace_prefix)
    renew(claim, session_factory, namespace)
    from .cert_monitor import forget
    with session_factory() as db:  # a retired layer serves nothing; its certificates are not monitored
        forget(db, claim.layer_id)
        db.commit()
    if not resources.exists('Namespace', namespace):
        finish(claim, 'Retired', 'StorageDeleted', 'Retired; the namespace and its retained storage were removed', 300, session_factory)
        return
    try:
        for document in manifests(claim.layer_id, claim.generation, claim.spec, config, claim.source_catalogs,
                                  claim.workers or _retired_workers(claim.spec), claim.workspaces, claim.kafka_workers):
            if document['kind'] == 'Deployment':
                document['spec']['replicas'] = 0
            renew(claim, session_factory)
            resources.apply(document)
        if claim.spec.kafka.enabled:
            if not retire_kafka(claim, resources, namespace, config):
                finish(claim, 'Provisioning', 'WaitingForKafkaPause',
                       'Waiting for Strimzi to pause before stopping brokers; storage is retained', 10, session_factory)
                return
    except ProvisioningError as exc:
        if exc.reason != 'ResourceTerminating':
            raise
        finish(claim, 'Retired', 'StorageDeleting', 'Retired; the namespace is being deleted', 30, session_factory)
        return
    finish(claim, 'Retired', 'StorageRetained',
        f'Retired: compute scaled to zero; volumes and policies retained in namespace {namespace}. Reactivate by applying '
        'the infrastructure with retired=false, or delete the namespace to remove the data permanently', 60, session_factory)


def reconcile(claim, resources, config, session_factory=SessionLocal):
    if claim.spec.retired:
        try:
            return retire(claim, resources, config, session_factory)
        except LostLease:
            return
        except ProvisioningError as exc:
            return finish(claim, 'Failed', exc.reason, exc.message, 0, session_factory)
        except (ApiException, httpx.HTTPError, OSError):
            return finish(claim, 'Retired', 'StorageRetained', 'Retirement check could not reach Kubernetes; retrying', 30, session_factory)
    try:
        documents = manifests(claim.layer_id, claim.generation, claim.spec, config, claim.source_catalogs, claim.workers,
                              claim.workspaces, claim.kafka_workers)
        if claim.spec.kafka.enabled:
            try:
                available = resources.request('GET', '/apis/kafka.strimzi.io/v1')
            except ApiException as exc:
                if exc.status == 404:
                    raise ProvisioningError('StrimziUnavailable', 'Install the supported Strimzi operator and v1 CRDs before enabling Kafka') from None
                raise
            kinds = {r.get('name') for r in available.get('resources', [])}
            if not {'kafkas', 'kafkanodepools', 'kafkatopics'} <= kinds:
                raise ProvisioningError('StrimziUnavailable', 'The installed Strimzi API lacks required v1 resources')
        waiting = False
        certificate_waiting = False
        issued = {}  # cert-manager Certificate name -> status
        namespace = documents[0]['metadata']['name']
        egress_problems = []
        for index, document in enumerate(documents):
            if document['kind'] == 'ResourceQuota':
                # Baseline isolation already exists; install data grants before compute.
                egress_problems = reconcile_egress(claim, resources, config, session_factory)
            renew(claim, session_factory, namespace)
            applied = resources.apply(document)
            if document['kind'] == 'Certificate':
                issued[document['metadata']['name']] = applied.get('status', {})
                certificate_waiting = certificate_waiting or not any(condition.get('type') == 'Ready' and condition.get('status') == 'True'
                    and condition.get('observedGeneration', 0) >= applied['metadata'].get('generation', 1)
                    for condition in applied.get('status', {}).get('conditions', []))
            if index == 0 and not claim.spec.trino.internal_secret:
                labels = {key: value for key, value in document['metadata']['labels'].items()
                          if not key.startswith('pod-security.')}
                resources.create_secret_once(namespace, INTERNAL_SECRET, labels,
                                             {'internal-secret': secrets.token_urlsafe(48)})
            if index == 0:
                labels = {key: value for key, value in document['metadata']['labels'].items()
                          if not key.startswith('pod-security.')}
                for name, values in generated_credentials(claim.spec):
                    resources.create_secret_once(namespace, name, labels, values())
                if claim.spec.kafka.enabled:
                    from .kafka_access import broker_credential
                    from .layer_kafka import POLICY_SECRET
                    resources.create_secret_once(namespace, POLICY_SECRET, labels,
                        {'token':broker_credential(claim.layer_id, config.kafka_policy_key)})
            if document['kind'] == 'Kafka':
                state = applied.get('status', {})
                waiting = waiting or not (state.get('observedGeneration', 0) >= applied['metadata'].get('generation', 1)
                    and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in state.get('conditions', [])))
            if document['kind'] == 'Deployment':
                for condition in applied.get('status', {}).get('conditions', []):
                    if condition.get('type') == 'Progressing' and condition.get('reason') == 'ProgressDeadlineExceeded':
                        raise ProvisioningError('StartupFailed', 'A layer deployment did not become ready before its startup deadline; check images, quota, volumes and pod events')
                waiting = not deployment_ready(applied) or waiting
        monitor_certificates(claim, resources, namespace, issued, session_factory)
        if claim.spec.kafka.enabled:
            reconcile_kafka_topics(claim, resources, namespace, config, session_factory)
        if egress_problems:
            reason = egress_problems[0]
            message = ('Data-source egress is closed: configure layerController.network podCidrs, serviceCidrs and nodeCidrs, then retry layer provisioning'
                       if reason == 'NetworkBoundaryMissing' else
                       'Data-source egress is closed: a target overlaps protected ranges, its CIDR/port is invalid, or cluster boundaries do not cover its address family; correct the source or network configuration and retry layer provisioning')
            finish(claim, 'Failed', reason, message, 0, session_factory)
        elif (certificate_waiting or waiting) and claim.ready:
            finish(claim, 'Ready', 'RollingUpdate',
                'Layer services are rolling out or renewing certificates; work stays admitted', 10, session_factory)
        elif certificate_waiting:
            finish(claim, 'Provisioning', 'WaitingForCertificate',
                'Waiting for cert-manager issuance; verify the layer issuer, DNS names and Certificate events', 10, session_factory)
        elif waiting:
            finish(claim, 'Provisioning', 'WaitingForRollout',
                'Waiting for layer services to roll out; check volumes, quota and pod events', 10, session_factory)
        elif claim.spec.trino.tls.mode != 'disabled' and not resources.verify_https('fdp-trino.%s.svc' % namespace):
            finish(claim, 'Provisioning', 'WaitingForVerifiedTLS',
                'Trino HTTPS is not verified; check the platform CA bundle, layer certificate DNS/validity and endpoint connectivity', 10, session_factory)
        elif claim.spec.kafka.enabled and not resources.verify_kafka('fdp-kafka-kafka-bootstrap.%s.svc' % namespace):
            finish(claim, 'Provisioning', 'WaitingForVerifiedKafkaTLS',
                'Kafka TLS is not verified; check trusted CA, broker certificate names and network policies', 10, session_factory)
        elif claim.spec.postgres.enabled and claim.spec.postgres.tls.mode != 'disabled' and not resources.verify_postgres('fdp-postgres.%s.svc' % namespace):
            finish(claim, 'Provisioning', 'WaitingForVerifiedPostgresTLS',
                'PostgreSQL TLS is not verified; check the platform CA bundle, database certificate DNS/validity and endpoint connectivity', 10, session_factory)
        else:
            finish(claim, 'Ready', 'Reconciled',
                'Layer services and artifact storage reconciled; SQL, workspaces, jobs and apps are available', 60, session_factory)
    except LostLease:
        return
    except ProvisioningError as exc:
        finish(claim, 'Failed', exc.reason, exc.message, 0, session_factory)
    except (ApiException, httpx.HTTPError, OSError) as exc:
        status = getattr(exc, 'status', 0)
        transient = not status or status in (404, 408, 429) or status >= 500
        # Never persist API response bodies, request contents, URLs or exception strings.
        finish(claim, 'Provisioning' if transient else 'Failed',
            'KubernetesUnavailable' if transient else 'KubernetesRejected',
            'Kubernetes request failed; verify controller permissions, resource ownership, API availability and admission policies',
            30 if transient else 0, session_factory)


def retire_kafka(claim, resources, namespace, config):
    """After pausing Strimzi, stop its pod set without changing the retained quorum/PVCs."""
    kafka = resources.request('GET', '/apis/kafka.strimzi.io/v1/namespaces/'+namespace+'/kafkas/fdp-kafka')
    if not any(c.get('type') == 'ReconciliationPaused' and c.get('status') == 'True'
               for c in kafka.get('status', {}).get('conditions', [])):
        return False
    selector = f'{OWNER}={config.installation},{LAYER}={claim.layer_id},{COMPONENT}=kafka'
    for root, plural in (('/apis/core.strimzi.io/v1', 'strimzipodsets'),
                         ('/apis/apps/v1', 'deployments')):
        path = root+'/namespaces/'+namespace+'/'+plural
        page = resources.api.call_api(path, 'GET', query_params=[('labelSelector', selector)])
        for item in page.get('items', []):
            meta = item['metadata']
            if any(meta.get('labels', {}).get(k) != v for k, v in
                   ((OWNER, config.installation), (LAYER, claim.layer_id), (COMPONENT, 'kafka'))):
                raise ProvisioningError('OwnershipConflict', 'Refusing to stop an unowned Kafka resource')
            # JSON Patch resourceVersion test prevents stopping a replaced object. No PVC deletion.
            field, value = ('pods', []) if plural == 'strimzipodsets' else ('replicas', 0)
            resources.api.call_api(path+'/'+meta['name'], 'PATCH', body=[
                {'op':'test','path':'/metadata/resourceVersion','value':meta['resourceVersion']},
                {'op':'replace','path':'/spec/'+field,'value':value}],
                header_params={'Content-Type':'application/json-patch+json','Accept':'application/json'})
    return True


def reconcile_kafka_topics(claim, resources, namespace, config, session_factory):
    from .layer_kafka import topic_manifest
    with session_factory() as db:
        identifiers = list(db.scalars(select(Resource.id).where(Resource.kind == 'topic',
            Resource.config['layer_id'].as_string() == claim.layer_id,
            Resource.config['phase'].as_string() != 'Deleted')))
    for ident in identifiers:
        renew(claim, session_factory, namespace)
        with session_factory() as db:
            topic = db.scalar(select(Resource).where(Resource.id == ident).with_for_update(skip_locked=True))
            if topic is None:
                continue
            document = topic_manifest(topic, namespace, config.installation)
            if topic.config.get('deleted'):
                # Deletion is an explicit, authorized API operation, never implicit layer cleanup.
                path = '/apis/kafka.strimzi.io/v1/namespaces/'+namespace+'/kafkatopics/'+document['metadata']['name']
                try:
                    found = resources.request('GET', path)
                    if any(found.get('metadata', {}).get('labels', {}).get(k) != document['metadata']['labels'][k] for k in (OWNER,LAYER)):
                        raise ProvisioningError('OwnershipConflict', 'Refusing to delete an unowned KafkaTopic')
                    resources.request('DELETE', path, {'apiVersion':'v1','kind':'DeleteOptions',
                        'preconditions':{'uid':found['metadata']['uid']}})
                    phase = 'Deleting'
                except ApiException as exc:
                    if exc.status != 404:
                        raise
                    phase = 'Deleted'
            else:
                result = resources.apply(document)
                state = result.get('status', {})
                ready = state.get('observedGeneration', 0) >= result.get('metadata', {}).get('generation', 1) and any(
                    c.get('type') == 'Ready' and c.get('status') == 'True' for c in state.get('conditions', []))
                phase = 'Ready' if ready else 'Pending'
            if topic.config.get('phase') != phase:
                topic.config = {**topic.config, 'phase':phase}
            db.commit()


def main():
    config = ControllerConfig(
        platform_namespace=os.environ['FDP_LAYER_PLATFORM_NAMESPACE'],
        installation=os.environ['FDP_LAYER_INSTALLATION'],
        namespace_prefix=os.environ['FDP_LAYER_NAMESPACE_PREFIX'],
        runtime_cluster_role=os.environ['FDP_LAYER_RUNTIME_CLUSTER_ROLE'],
        dns_namespace=os.environ.get('FDP_LAYER_DNS_NAMESPACE', 'kube-system'),
        artifact_image=os.environ.get('FDP_LAYER_ARTIFACT_IMAGE', ''),
        shared_database_namespace=os.environ.get('FDP_LAYER_SHARED_DATABASE_NAMESPACE', ''),
        image_pull_secret=os.environ.get('FDP_LAYER_IMAGE_PULL_SECRET', ''),
        kafka_policy_key=os.environ.get('FDP_SESSION_SIGNING_KEY', ''),
        kafka_api_server_cidrs=tuple(json.loads(os.environ.get('FDP_LAYER_KAFKA_API_SERVER_CIDRS', '[]'))),
        egress_proxy=os.environ.get('FDP_LAYER_EGRESS_PROXY', '') == 'true',
        cilium=os.environ.get('FDP_LAYER_CILIUM', '') == 'true',
        trust_file=os.environ.get('FDP_LAYER_TRUST_FILE', ''),
        pod_cidrs=tuple(json.loads(os.environ.get('FDP_LAYER_POD_CIDRS', '[]'))),
        service_cidrs=tuple(json.loads(os.environ.get('FDP_LAYER_SERVICE_CIDRS', '[]'))),
        node_cidrs=tuple(json.loads(os.environ.get('FDP_LAYER_NODE_CIDRS', '[]'))),
        protected_cidrs=tuple(json.loads(os.environ.get('FDP_LAYER_PROTECTED_CIDRS', '[]'))))
    resources = KubernetesResources()
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    READY_FILE.unlink(missing_ok=True)
    while not stopping:
        claim = None
        try:
            claim = claim_operation()
            if claim:
                reconcile(claim, resources, config)
            READY_FILE.touch()
        except Exception:
            # Lease expiry makes interrupted work reclaimable. Keep arbitrary driver
            # exceptions out of logs: connection strings can contain credentials.
            READY_FILE.unlink(missing_ok=True)
            print('Layer reconciliation interrupted; operation will resume after lease expiry', flush=True)
        for _ in range(2 if claim else 10):
            if stopping:
                break
            time.sleep(1)
    READY_FILE.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
