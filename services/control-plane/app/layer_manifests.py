"""Deterministic layer resources. No credential values or cross-namespace mounts."""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import re

from .layer_infrastructure import MIN_WORKER_MEMORY_GB, InfrastructureSpec, namespace_for

OWNER = 'fdp.floren.app/installation'
# Generated once per layer by the controller; only that layer's Trino nodes read it.
INTERNAL_SECRET = 'fdp-trino-internal'
# Bearer token the coordinator sends completed-query events with (query log); generated once per layer.
EVENTS_SECRET = 'fdp-trino-events'
ARTIFACT_CLAIM = 'fdp-artifacts'
ARTIFACT_HOLDER = 'fdp-artifacts'
ARTIFACT_ROOT = '/artifacts'
TRUST_CONFIGMAP = 'fdp-trust'
LAYER = 'fdp.floren.app/layer'
COMPONENT = 'app.kubernetes.io/component'

# Native automatic TLS covers node IPs, not a virtual Service IP. Resolve the
# headless coordinator endpoint on each worker start; restart after IP replacement.
# No Kubernetes API token or additional secret distribution is required.
WORKER_START = '''#!/bin/bash
set -euo pipefail
while true; do
  answers=$(timeout -k 1 5 getent ahosts "$TRINO_DISCOVERY_HOST") || answers=''
  address=''
  read -r address _ <<< "$answers" || true
  if [ -n "$address" ]; then break; fi
  sleep 2
done
mkdir -p /data/trino
printf '%s\\n' "$address" > /data/trino/discovery-address
case "$address" in
  *:*) export TRINO_DISCOVERY_URI="https://[$address]:8443" ;;
  *) export TRINO_DISCOVERY_URI="https://$address:8443" ;;
esac
exec /usr/lib/trino/bin/launcher run --etc-dir /etc/trino
'''

# Java source for the worker's internal bearer: an HS256 JWT over a key derived from the node secret
# (HKDF-SHA256, as Trino derives it), or with "legacy" the SHA-256 key and HS512 of older releases.
# It authenticates the worker's own management request to itself and never leaves the pod.
INTERNAL_TOKEN = '''import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Base64;
import javax.crypto.KDF;
import javax.crypto.Mac;
import javax.crypto.spec.HKDFParameterSpec;
import javax.crypto.spec.SecretKeySpec;

public class InternalToken {
    public static void main(String[] args) throws Exception {
        byte[] secret = System.getenv("TRINO_INTERNAL_SECRET").getBytes(StandardCharsets.UTF_8);
        boolean legacy = args.length > 0 && args[0].equals("legacy");
        String algorithm = legacy ? "HmacSHA512" : "HmacSHA256";
        byte[] key = legacy ? MessageDigest.getInstance("SHA-256").digest(secret)
            : KDF.getInstance("HKDF-SHA256").deriveKey(algorithm, HKDFParameterSpec.ofExtract().addIKM(secret)
                .thenExpand("internal-communication".getBytes(StandardCharsets.UTF_8), 32)).getEncoded();
        Base64.Encoder base64 = Base64.getUrlEncoder().withoutPadding();
        String header = base64.encodeToString(("{\\"alg\\":\\"" + (legacy ? "HS512" : "HS256") + "\\"}").getBytes(StandardCharsets.UTF_8));
        String claims = base64.encodeToString(("{\\"sub\\":\\"fdp-drain\\",\\"exp\\":" + (System.currentTimeMillis() / 1000 + 300) + "}")
            .getBytes(StandardCharsets.UTF_8));
        Mac mac = Mac.getInstance(algorithm);
        mac.init(new SecretKeySpec(key, algorithm));
        String signed = header + "." + claims;
        System.out.print(signed + "." + base64.encodeToString(mac.doFinal(signed.getBytes(StandardCharsets.UTF_8))));
    }
}
'''

# preStop of a worker being removed: stop taking work, finish running queries, then Trino exits by itself.
# The pod's termination grace period (trino.workers.drain_seconds) bounds the wait.
WORKER_DRAIN = '''#!/bin/bash
set -u
url=%s
drain() {
  token=$(java /etc/trino/InternalToken.java "$@") || return 1
  [ "$(curl -sk -o /dev/null -w '%%{http_code}' -X PUT -H "X-Trino-Internal-Bearer: $token" \\
       -H 'Content-Type: application/json' --data '"SHUTTING_DOWN"' "$url/v1/info/state")" = 200 ]
}
drain || drain legacy || exit 0
while curl -sk -o /dev/null --max-time 5 "$url/v1/info"; do sleep 2; done
'''

WORKER_HEALTH = '''#!/bin/bash
set -euo pipefail
expected=$(cat /data/trino/discovery-address)
addresses=$(timeout -k 1 2 getent ahosts "$TRINO_DISCOVERY_HOST") || exit 0
# A temporary DNS outage must not restart otherwise healthy workers. A resolved
# endpoint that no longer contains our coordinator address requires a fresh start.
if [ -z "$addresses" ]; then exit 0; fi
while read -r address _; do
  if [ "$address" = "$expected" ]; then exit 0; fi
done <<< "$addresses"
exit 1
'''


@dataclass(frozen=True)
class SecretRef:
    name: str
    key: str


class ProvisioningError(Exception):
    """Only static, credential-free reasons/messages may reach operation status."""
    def __init__(self, reason, message):
        self.reason, self.message = reason, message
        super().__init__(message)


@dataclass(frozen=True)
class ControllerConfig:
    platform_namespace: str
    installation: str
    namespace_prefix: str
    runtime_cluster_role: str
    dns_namespace: str = 'kube-system'
    # Image of the per-layer artifact holder (needs sh, find, head, base64, stat).
    artifact_image: str = ''
    # Platform public CA bundle (mounted file) deployed to every layer for its executions.
    trust_file: str = ''
    pod_cidrs: tuple = ()
    service_cidrs: tuple = ()
    node_cidrs: tuple = ()
    protected_cidrs: tuple = ()
    # Registry pull secret the control plane writes into each layer namespace (empty: public images).
    image_pull_secret: str = ''
    # The platform runs the workspace egress proxy: VS Code and RStudio may reach it (and through it, allowed hosts).
    egress_proxy: bool = False
    shared_database_namespace: str = ""

    def __post_init__(self):
        for value in (self.platform_namespace, self.installation, self.namespace_prefix,
                      self.runtime_cluster_role, self.dns_namespace):
            if not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', value) or len(value) > 63:
                raise ValueError('controller installation settings must be DNS labels')
        if self.shared_database_namespace and not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', self.shared_database_namespace):
            raise ValueError('shared database namespace must be a DNS label')
        namespace_for('012345abcdef', self.namespace_prefix)
        from .layer_egress import NetworkBoundary
        NetworkBoundary(self.pod_cidrs, self.service_cidrs, self.node_cidrs, self.protected_cidrs)


@dataclass(frozen=True)
class WorkerPlan:
    """Trino workers as the controller resolved them: the warehouse's size and how many run now."""
    warehouse: str
    cpu: float
    memory_gb: float
    replicas: int


def worker_plan(db, spec, scale=None):
    """The workers of a layer from its worker warehouse; None when that warehouse does not exist.
    scale: the layer's recorded worker count (autoscaling), kept within the configured min and max."""
    from sqlalchemy import select
    from .models import ComputePool
    workers = spec.trino.workers
    pool = db.scalar(select(ComputePool).where(ComputePool.name == workers.warehouse)) if workers.warehouse else None
    if pool is None:
        return None
    return WorkerPlan(pool.name, pool.cpu, pool.memory_gb, min(workers.max, max(workers.min, scale or workers.min)))


def heap(memory_bytes):
    """JVM heap for a Trino node: 70% of its memory leaves room for the JVM itself."""
    return '%dM' % (memory_bytes * 7 // 10 // 2**20)


def _cpu(value):
    # Canonical form, as the API server stores it, so repeated applies never churn.
    return '%dm' % round(value * 1000)


def _bytes(quantity):
    units = {'Mi': 2**20, 'Gi': 2**30, 'Ti': 2**40, 'M': 2**20, 'G': 2**30}
    number, unit = re.fullmatch(r'([0-9]+)(Mi|Gi|Ti|M|G)', quantity).groups()
    return int(number) * units[unit]


def public_trust_pem(path):
    """Re-read on every reconcile so a rotated platform bundle reaches each layer."""
    from cryptography.hazmat.primitives.serialization import Encoding
    from pathlib import Path
    from .java_trust import trusted_certificates
    try:
        certificates = trusted_certificates(Path(path).read_bytes())
    except (OSError, ValueError):
        raise ProvisioningError('InvalidTrustBundle', 'The platform CA bundle must contain only valid PEM CA certificates; fix trust.caBundleSecret') from None
    return ''.join(c.certificate.public_bytes(Encoding.PEM).decode() for c in certificates)


CATALOG_CONFIGMAP = 'fdp-trino-catalogs'
GENERATED_POSTGRES_CREDENTIALS = 'fdp-postgres-credentials'
GENERATED_RUSTFS_CREDENTIALS = 'fdp-rustfs-credentials'


def with_credentials(spec):
    """The spec with generated credential Secret names filled in where none was given."""
    spec = spec.model_copy(deep=True)
    if spec.postgres.enabled and not spec.postgres.credentials_secret:
        spec.postgres.credentials_secret = GENERATED_POSTGRES_CREDENTIALS
    if spec.rustfs.enabled and not spec.rustfs.credentials_secret:
        spec.rustfs.credentials_secret = GENERATED_RUSTFS_CREDENTIALS
    return spec


def generated_credentials(spec):
    """Secrets the controller creates once in the layer namespace; existing ones are kept."""
    import secrets
    result = []
    if spec.postgres.enabled and not spec.postgres.credentials_secret:
        result.append((GENERATED_POSTGRES_CREDENTIALS, lambda: {
            'postgres-password': secrets.token_urlsafe(32), 'engine-password': secrets.token_urlsafe(32)}))
    if spec.rustfs.enabled and not spec.rustfs.credentials_secret:
        result.append((GENERATED_RUSTFS_CREDENTIALS, lambda: {
            'access-key': 'fdp-' + secrets.token_hex(8), 'secret-key': secrets.token_urlsafe(32)}))
    result.append((EVENTS_SECRET, lambda: {'events-token': secrets.token_urlsafe(32)}))
    return result


def generated_catalogs(spec, namespace):
    """Catalogs for the layer's own PostgreSQL and Iceberg storage. They hold no credentials:
    Trino resolves ${ENV:...} from namespace-local Secrets the controller never reads."""
    catalogs, env = {}, []
    pg = spec.postgres
    if pg.catalog or spec.iceberg.enabled:
        env.append({'name': 'FDP_POSTGRES_ENGINE_PASSWORD',
                    'valueFrom': {'secretKeyRef': {'name': pg.credentials_secret, 'key': 'engine-password'}}})
    def jdbc(database):
        url = 'jdbc:postgresql://fdp-postgres.%s.svc:5432/%s' % (namespace, database)
        if pg.tls.mode != 'disabled':
            url += '?sslmode=verify-full&sslfactory=org.postgresql.ssl.DefaultJavaSSLFactory'
        return url
    if pg.catalog:
        catalogs[pg.catalog + '.properties'] = (
            'connector.name=postgresql\nconnection-url=%s\nconnection-user=fdp_engine\n'
            'connection-password=${ENV:FDP_POSTGRES_ENGINE_PASSWORD}\n' % jdbc('fdp'))
    if spec.iceberg.enabled:
        from .layer_rustfs import engine_env
        env.extend(engine_env(spec.rustfs))
        catalogs[spec.iceberg.catalog + '.properties'] = (
            'connector.name=iceberg\niceberg.catalog.type=jdbc\n'
            'iceberg.jdbc-catalog.catalog-name=%s\n' % spec.iceberg.catalog +
            'iceberg.jdbc-catalog.driver-class=org.postgresql.Driver\n'
            'iceberg.jdbc-catalog.connection-url=%s\n' % jdbc('iceberg') +
            'iceberg.jdbc-catalog.connection-user=fdp_engine\n'
            'iceberg.jdbc-catalog.connection-password=${ENV:FDP_POSTGRES_ENGINE_PASSWORD}\n'
            'iceberg.jdbc-catalog.default-warehouse-dir=s3://%s/\n' % spec.iceberg.bucket +
            'fs.s3.enabled=true\ns3.endpoint=%s://fdp-rustfs.%s.svc:9000\n' % (
                'https' if spec.rustfs.tls.mode != 'disabled' else 'http', namespace) +
            's3.region=us-east-1\ns3.path-style-access=true\n'
            's3.aws-access-key=${ENV:FDP_RUSTFS_ACCESS_KEY}\ns3.aws-secret-key=${ENV:FDP_RUSTFS_SECRET_KEY}\n'
            # Access is decided by the platform's policy; these procedures could read arbitrary files.
            'iceberg.security=SYSTEM\niceberg.register-table-procedure.enabled=false\n'
            'iceberg.add-files-procedure.enabled=false\n')
    return catalogs, env


def catalog_volume(operator_secret, generated, external=False):
    from .source_catalogs import SECRET
    sources = ([{'secret': {'name': operator_secret}}] if operator_secret else []) + \
              ([{'configMap': {'name': CATALOG_CONFIGMAP}}] if generated else []) + \
              ([{'secret': {'name': SECRET, 'optional': True}}] if external else [])
    if not sources:
        return {'emptyDir': {}}
    if len(sources) == 1 and operator_secret:
        return {'secret': {'secretName': operator_secret}}  # unchanged for existing layers
    return {'projected': {'sources': sources}}


def manifests(layer_id, generation, spec: InfrastructureSpec, config: ControllerConfig, source_catalogs='',
              workers: 'WorkerPlan | None' = None):
    """source_catalogs: digest of the control-plane-written external source catalogs ('' for none).
    workers: the resolved worker warehouse (worker_plan); required."""
    """Order is intentional: isolation/budgets precede identities and compute."""
    if not spec.enabled:
        raise ProvisioningError('UnmanagedLayer', 'Layer is not managed by the controller')
    spec = with_credentials(spec)
    # Fail before making any Kubernetes changes for features not yet implemented.
    if spec.postgres.enabled and spec.postgres.tls.trust_bundle:
        raise ProvisioningError('UnsupportedPostgresTrust', 'PostgreSQL server trust_bundle is not supported; configure client trust in Trino and the platform CA bundle')
    if spec.storage.artifact_secret:
        raise ProvisioningError('UnsupportedArtifacts', 'Object-storage layer artifacts are not implemented yet; omit artifact_secret to use the layer volume')
    if not config.artifact_image:
        raise ProvisioningError('ArtifactImageMissing', 'The controller has no artifact holder image configured; set it in the Helm chart')
    if workers is None:
        raise ProvisioningError('WorkerWarehouseMissing',
                                'Choose an existing warehouse for the Trino workers (trino.workers.warehouse)')
    if workers.memory_gb < MIN_WORKER_MEMORY_GB:
        raise ProvisioningError('WorkerWarehouseTooSmall',
                                'Trino workers need a warehouse with at least %s GB memory' % MIN_WORKER_MEMORY_GB)
    tls = spec.trino.tls.mode != 'disabled'
    coordinator_memory = _bytes(spec.trino.coordinator.memory)
    worker_memory = int(workers.memory_gb * 2**30) // 2**20 * 2**20
    peak = spec.trino.workers.max  # the quota must hold the layer at its largest
    pg = spec.postgres.enabled
    pg_tls = pg and spec.postgres.tls.mode != 'disabled'
    s3 = spec.rustfs.enabled
    s3_tls = s3 and spec.rustfs.tls.mode != 'disabled'  # adds the restart sidecar (100m / 128Mi limits)
    pg_databases = ('iceberg',) if spec.iceberg.enabled else ()
    pg_sidecars = int(pg_tls) + int(bool(pg_databases))  # 100m / 128Mi limits each
    if (spec.trino.coordinator.cpu + workers.cpu * peak + (0.1 if tls else 0) * (1 + peak) + 0.2 +
            (spec.postgres.cpu + 0.1 * pg_sidecars if pg else 0) + (spec.rustfs.cpu + 0.1 + 0.1 * s3_tls if s3 else 0) > spec.quota.cpu or
            coordinator_memory + worker_memory * peak + (128 * 2**20 if tls else 0) * (1 + peak) + 128 * 2**20 +
            (_bytes(spec.postgres.memory) + 128 * 2**20 * pg_sidecars if pg else 0) +
            (_bytes(spec.rustfs.memory) + 128 * 2**20 * (1 + s3_tls) if s3 else 0) > _bytes(spec.quota.memory) or
            1 + peak + 1 + int(pg) + int(s3) > spec.quota.pods or
            _bytes(spec.storage.artifact_size) + (_bytes(spec.postgres.size) if pg else 0) +
            (_bytes(spec.rustfs.size) if s3 else 0) > _bytes(spec.quota.storage) or
            1 + int(pg) + int(s3) > spec.quota.persistent_volume_claims):
        raise ProvisioningError('InsufficientQuota', 'The layer quota must hold the Trino coordinator and the maximum number of workers')

    namespace = namespace_for(layer_id, config.namespace_prefix)
    hostname = 'fdp-trino.%s.svc' % namespace
    labels = {OWNER: config.installation, LAYER: layer_id, 'app.kubernetes.io/managed-by': 'fdp-layer-controller'}

    def obj(kind, name, body, api='v1', namespaced=True):
        metadata = {'name': name, 'labels': dict(labels)}
        if namespaced:
            metadata['namespace'] = namespace
        return {'apiVersion': api, 'kind': kind, 'metadata': metadata, **body}

    result = [obj('Namespace', namespace, {}, namespaced=False)]
    result[0]['metadata']['labels'].update({
        # baseline, not restricted: the RStudio and SQL editor images start their init as
        # root before dropping to uid 1000. Restricted violations are still warned and audited.
        'pod-security.kubernetes.io/enforce': 'baseline',
        'pod-security.kubernetes.io/enforce-version': 'v1.31',
        'pod-security.kubernetes.io/audit': 'restricted',
        'pod-security.kubernetes.io/warn': 'restricted',
    })
    result[0]['metadata']['annotations'] = {'fdp.floren.app/retention': 'Retain'}

    def policy(name, selector, **rules):
        result.append(obj('NetworkPolicy', name, {'spec': {'podSelector': {'matchLabels': selector},
            'policyTypes': [key.title() for key in rules], **rules}}, 'networking.k8s.io/v1'))

    def peer(ns, selector):
        # Both selectors must match the SAME peer, never separate OR entries.
        return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': ns}},
                'podSelector': {'matchLabels': selector}}

    trino = {LAYER: layer_id, COMPONENT: 'trino'}
    opa = 'http://fdp-control-plane.%s.svc:8000/internal/trino/' % config.platform_namespace
    policy('default-deny', {}, ingress=[], egress=[])
    dns = peer(config.dns_namespace, {'k8s-app': 'kube-dns'})
    policy('dns', {}, egress=[{'to': [dns], 'ports': [
        {'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}])
    cp = peer(config.platform_namespace, {'app.kubernetes.io/name': 'fdp-control-plane'})
    controller = peer(config.platform_namespace, {'app.kubernetes.io/name': 'fdp-layer-controller'})
    policy('control-plane-egress', {}, egress=[{'to': [cp], 'ports': [{'protocol': 'TCP', 'port': 8000}]}])
    if config.egress_proxy:
        proxy = peer(config.platform_namespace, {'app.kubernetes.io/name': 'fdp-egress-proxy'})
        policy('workspace-egress-proxy', {'fdp.floren.app/egress': 'proxy'},
               egress=[{'to': [proxy], 'ports': [{'protocol': 'TCP', 'port': 3128}]}])
    policy('trino-gateway', {**trino, 'fdp.floren.app/trino-role': 'coordinator'}, ingress=[
        {'from': [cp, controller] if tls else [cp], 'ports': [{'protocol': 'TCP', 'port': 8443 if tls else 8080}]}])
    # Editors and apps are reached only through the platform gateway, plus the control
    # plane's own readiness checks (it polls each editor's health endpoint directly).
    gateway = peer(config.platform_namespace, {'app.kubernetes.io/name': 'traefik'})
    policy('gateway-ingress', {'fdp.floren.app/managed': 'true'}, ingress=[{'from': [gateway, cp]}])
    # Exchange/discovery traffic is restricted to this layer's Trino pods.
    local_trino = {'podSelector': {'matchLabels': trino}}
    policy('trino-internal', trino,
           ingress=[{'from': [local_trino], 'ports': [{'protocol': 'TCP', 'port': 8443 if tls else 8080}]}],
           egress=[{'to': [local_trino], 'ports': [{'protocol': 'TCP', 'port': 8443 if tls else 8080}]}])
    # Distributed Airflow: components reach their deployment's broker and metadata database. Each
    # deployment's backends require its own passwords; other pods in the layer cannot reach them.
    backends = {'podSelector': {'matchExpressions': [
        {'key': 'fdp.floren.app/role', 'operator': 'In', 'values': ['broker', 'airflow-metadata']}]}}
    components = {'podSelector': {'matchExpressions': [{'key': 'fdp.floren.app/airflow-role', 'operator': 'Exists'}]}}
    backend_ports = [{'protocol': 'TCP', 'port': 6379}, {'protocol': 'TCP', 'port': 5432}]
    for name, selector, rules in (
            ('airflow-backends', backends['podSelector'], {'ingress': [{'from': [components], 'ports': backend_ports}]}),
            ('airflow-components', components['podSelector'], {'egress': [{'to': [backends], 'ports': backend_ports}]})):
        result.append(obj('NetworkPolicy', name, {'spec': {'podSelector': selector,
            'policyTypes': [key.title() for key in rules], **rules}}, 'networking.k8s.io/v1'))
    if config.shared_database_namespace:
        database = peer(config.shared_database_namespace, {'app.kubernetes.io/name': 'fdp-metadata-db'})
        result.append(obj('NetworkPolicy', 'airflow-shared-metadata', {'spec': {
            'podSelector': {'matchExpressions': [{'key': 'fdp.floren.app/airflow-role', 'operator': 'In',
                'values': ['standalone', 'scheduler', 'web', 'worker']}]},
            'policyTypes': ['Egress'],
            'egress': [{'to': [database], 'ports': [{'protocol': 'TCP', 'port': 5432}]}] }}, 'networking.k8s.io/v1'))
    if pg:
        postgres = {LAYER: layer_id, COMPONENT: 'postgres'}
        policy('postgres-ingress', postgres, ingress=[{'from': [local_trino, cp, controller],
            'ports': [{'protocol': 'TCP', 'port': 5432}]}])
        policy('postgres-trino-egress', trino, egress=[{'to': [{'podSelector': {'matchLabels': postgres}}],
            'ports': [{'protocol': 'TCP', 'port': 5432}]}])
    if s3:
        rustfs = {LAYER: layer_id, COMPONENT: 'rustfs'}
        policy('rustfs-ingress', rustfs, ingress=[{'from': [local_trino, cp], 'ports': [{'protocol': 'TCP', 'port': 9000}]}])
        policy('rustfs-trino-egress', trino, egress=[{'to': [{'podSelector': {'matchLabels': rustfs}}],
            'ports': [{'protocol': 'TCP', 'port': 9000}]}])
    result.append(obj('ResourceQuota', 'layer-budget', {'spec': {'hard': {
        'requests.cpu': _cpu(spec.quota.cpu), 'limits.cpu': _cpu(spec.quota.cpu),
        'requests.memory': spec.quota.memory, 'limits.memory': spec.quota.memory,
        'pods': str(spec.quota.pods), 'persistentvolumeclaims': str(spec.quota.persistent_volume_claims),
        'requests.storage': spec.quota.storage}}}))
    result.append(obj('LimitRange', 'container-defaults', {'spec': {'limits': [{
        'type': 'Container', 'default': {'cpu': '1', 'memory': '1Gi'},
        'defaultRequest': {'cpu': '250m', 'memory': '256Mi'}}]}}))
    result.append(obj('ServiceAccount', 'fdp-execution', {'automountServiceAccountToken': False}))
    result.append(obj('RoleBinding', 'fdp-runtime', {
        'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': config.runtime_cluster_role},
        'subjects': [{'kind': 'ServiceAccount', 'name': 'fdp-control-plane', 'namespace': config.platform_namespace}],
    }, 'rbac.authorization.k8s.io/v1'))
    if pg:
        from .layer_postgres import manifests as postgres_manifests
        result.extend(postgres_manifests(spec.postgres, namespace, obj, pg_databases))
    if s3:
        from .layer_rustfs import manifests as rustfs_manifests
        result.extend(rustfs_manifests(spec.rustfs, namespace, obj, config.artifact_image, spec.trino.tls.trust_bundle))
    catalogs, engine_env = generated_catalogs(spec, namespace)
    if catalogs:
        result.append(obj('ConfigMap', CATALOG_CONFIGMAP, {'data': catalogs}))

    common = (('' if tls else 'http-server.http.port=8080\n') +
        'http-server.authentication.type=JWT\n'
        'http-server.authentication.allow-insecure-over-http=%s\n' % ('false' if tls else 'true') +
        'http-server.authentication.jwt.key-file=' + opa + 'jwks\n'
        'http-server.authentication.jwt.required-issuer=fdp-sql\n'
        'http-server.authentication.jwt.required-audience=trino-layer-' + layer_id + '\n'
        'internal-communication.shared-secret=${ENV:TRINO_INTERNAL_SECRET}\n'
        'discovery.uri=%s\n' % ('${ENV:TRINO_DISCOVERY_URI}' if tls else 'http://fdp-trino:8080') +
        'query.max-run-time=1h\nprotocol.spooling.enabled=false\n')
    if tls:
        common += ('http-server.http.enabled=false\ninternal-communication.https.required=true\n'
                   'http-server.https.enabled=true\nhttp-server.https.port=8443\n'
                   'http-server.https.keystore.path=/tls-runtime/keystore.pem\n'
                   'http-server.https.ssl-context.refresh-time=10s\n')
        result.append(obj('Service', 'fdp-trino-discovery', {'spec': {
            'clusterIP': 'None', 'publishNotReadyAddresses': True,
            'selector': {**trino, 'fdp.floren.app/trino-role': 'coordinator'},
            'ports': [{'name': 'https', 'port': 8443, 'targetPort': 'https'}]}}))
    if spec.trino.tls.mode == 'cert_manager':
        certificate = spec.trino.tls
        result.append(obj('Certificate', 'fdp-trino', {'spec': {
            'secretName': certificate.secret_name, 'dnsNames': [hostname],
            'duration': '%dh' % certificate.duration_hours, 'renewBefore': '%dh' % certificate.renew_before_hours,
            'privateKey': {'rotationPolicy': 'Always'}, 'issuerRef': certificate.issuer.model_dump(),
            'usages': ['digital signature', 'key encipherment', 'server auth'],
        }}, 'cert-manager.io/v1'))
    # Layer-bound policy paths: a query admitted for another engine is always denied.
    policy_root = opa + 'layers/' + layer_id + '/'
    access = ('access-control.name=opa\nopa.policy.uri=' + policy_root + 'allow\n'
        'opa.policy.batched-uri=' + policy_root + 'batch\nopa.policy.row-filters-uri=' + policy_root + 'row-filters\n'
        'opa.identity.extra-credentials-keys=dc_context\n'
        'opa.allow-permission-management-operations=false\nopa.log-requests=false\nopa.log-responses=false\n')
    sizes = {'coordinator': (spec.trino.coordinator.cpu, coordinator_memory), 'worker': (workers.cpu, worker_memory)}
    for role, replicas in [('coordinator', 1), ('worker', workers.replicas)]:
        name = 'fdp-trino-' + role
        cpu, memory = sizes[role]
        node_labels = {**labels, **trino, 'fdp.floren.app/trino-role': role}
        pod_labels = {**node_labels, **({'fdp.floren.app/pool': workers.warehouse} if role == 'worker' else {})}
        data = {'config.properties': 'coordinator=%s\n' % ('true' if role == 'coordinator' else 'false') +
                ('node-scheduler.include-coordinator=false\n'
                 # Running and waiting queries on /metrics, read by the platform's worker scaler.
                 'openmetrics.jmx-object-names=trino.execution:name=QueryManager\n' if role == 'coordinator' else '') + common,
            'node.properties': 'node.environment=layer_%s\nnode.data-dir=/data/trino\n' % layer_id,
            'jvm.config': '-server\n-Xmx%s\n-XX:+UseG1GC\n-XX:+ExitOnOutOfMemoryError\n' % heap(memory),
            'access-control.properties': access}
        if role == 'coordinator':
            # Every completed query goes to the platform's query log (who or what ran it, tables, duration).
            data['event-listener.properties'] = (
                'event-listener.name=http\nhttp-event-listener.log-completed=true\n'
                'http-event-listener.log-created=false\n'
                'http-event-listener.connect-ingest-uri=http://fdp-control-plane.%s.svc:8000/internal/trino/layers/%s/events\n'
                'http-event-listener.connect-http-headers=Authorization:Bearer ${ENV:FDP_TRINO_EVENTS_TOKEN}\n'
                'http-event-listener.connect-retry-count=5\n' % (config.platform_namespace, layer_id))
        if role == 'worker':
            data['config.properties'] += 'shutdown.grace-period=15s\n'
            data.update({'InternalToken.java': INTERNAL_TOKEN,
                         'worker-drain.sh': WORKER_DRAIN % ('https://127.0.0.1:8443' if tls else 'http://127.0.0.1:8080')})
        if tls and role == 'worker':
            data.update({'worker-start.sh': WORKER_START, 'worker-health.sh': WORKER_HEALTH})
        trust = spec.trino.tls.trust_bundle
        if trust:
            data['jvm.config'] += ('-Djavax.net.ssl.trustStore=/java-trust/truststore.p12\n'
                                  '-Djavax.net.ssl.trustStoreType=PKCS12\n'
                                  '-Djavax.net.ssl.trustStorePassword=changeit\n')
        result.append(obj('ConfigMap', name, {'data': data}))
        internal = spec.trino.internal_secret or SecretRef(INTERNAL_SECRET, 'internal-secret')
        volumes = [
            {'name': 'config', 'configMap': {'name': name}},
            {'name': 'catalogs', **catalog_volume(spec.trino.catalog_secret, bool(catalogs), bool(source_catalogs))},
            {'name': 'data', 'emptyDir': {}},
        ]
        container = {'name': 'trino', 'image': spec.trino.image,
            'ports': [{'name': 'https' if tls else 'http', 'containerPort': 8443 if tls else 8080}],
            'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
            'env': [{'name': 'TRINO_INTERNAL_SECRET', 'valueFrom': {'secretKeyRef': {'name': internal.name, 'key': internal.key}}},
                    *engine_env],
            'volumeMounts': [{'name': 'config', 'mountPath': '/etc/trino', 'readOnly': True},
                {'name': 'catalogs', 'mountPath': '/etc/trino/catalog', 'readOnly': True},
                {'name': 'data', 'mountPath': '/data/trino'}],
            'resources': {kind: {'cpu': _cpu(cpu), 'memory': '%dMi' % (memory // 2**20)} for kind in ('requests', 'limits')},
            'startupProbe': {'tcpSocket': {'port': 'https' if tls else 'http'}, 'periodSeconds': 5, 'failureThreshold': 60},
            'readinessProbe': {'tcpSocket': {'port': 'https' if tls else 'http'}},
        }
        if role == 'coordinator':
            container['env'].append({'name': 'FDP_TRINO_EVENTS_TOKEN',
                                     'valueFrom': {'secretKeyRef': {'name': EVENTS_SECRET, 'key': 'events-token'}}})
        if role == 'worker':
            container['lifecycle'] = {'preStop': {'exec': {'command': ['bash', '/etc/trino/worker-drain.sh']}}}
        if tls:
            if role == 'coordinator':
                container['env'].append({'name': 'TRINO_DISCOVERY_URI', 'value': 'https://127.0.0.1:8443'})
            else:
                container['env'].append({'name': 'TRINO_DISCOVERY_HOST',
                    'value': 'fdp-trino-discovery.%s.svc' % namespace})
                container['command'] = ['bash', '/etc/trino/worker-start.sh']
                container['livenessProbe'] = {'exec': {'command': ['bash', '/etc/trino/worker-health.sh']},
                    'periodSeconds': 10, 'timeoutSeconds': 5, 'failureThreshold': 3}
        tls_containers, init_containers = [], []
        if trust:
            volumes.extend([{'name': 'java-ca', 'secret': {'secretName': trust.name,
                'items': [{'key': trust.key, 'path': 'ca.pem'}]}},
                {'name': 'java-trust', 'emptyDir': {'medium': 'Memory'}}])
            container['volumeMounts'].append({'name': 'java-trust', 'mountPath': '/java-trust', 'readOnly': True})
            init_containers.append({'name': 'java-trust-init', 'image': config.artifact_image,
                'command': ['python', '-m', 'app.java_trust', '--input', '/java-ca/ca.pem',
                            '--output', '/java-trust/truststore.p12'],
                'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '128Mi'}},
                'volumeMounts': [{'name': 'java-ca', 'mountPath': '/java-ca', 'readOnly': True},
                                {'name': 'java-trust', 'mountPath': '/java-trust'}]})
        if tls:
            volumes.extend([{'name': 'tls-input', 'secret': {'secretName': spec.trino.tls.secret_name}},
                            {'name': 'tls-runtime', 'emptyDir': {'medium': 'Memory'}}])
            container['volumeMounts'].append({'name': 'tls-runtime', 'mountPath': '/tls-runtime', 'readOnly': True})
            writer = {'name': 'tls-bundle', 'image': config.artifact_image,
                'command': ['python', '-m', 'app.tls_bundle', '--input', '/tls-input',
                    '--output', '/tls-runtime/keystore.pem', '--hostname', hostname],
                'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '128Mi'}},
                'volumeMounts': [{'name': 'tls-input', 'mountPath': '/tls-input', 'readOnly': True},
                    {'name': 'tls-runtime', 'mountPath': '/tls-runtime'}]}
            init_containers.append({**deepcopy(writer), 'name': 'tls-init', 'command': [*writer['command'], '--once']})
            tls_containers = [{**deepcopy(writer), 'readinessProbe': {'exec': {'command': ['python', '-c',
                "import os,time; assert time.time()-os.stat('/tls-runtime/ready').st_mtime < 30"]}, 'periodSeconds': 5}}]
        result.append(obj('Deployment', name, {'spec': {'replicas': replicas,
            'strategy': {'type': 'Recreate'}, 'progressDeadlineSeconds': 600,
            'selector': {'matchLabels': node_labels}, 'template': {
                'metadata': {'labels': pod_labels, 'annotations': {
                    'fdp.floren.app/config-hash': hashlib.sha256(json.dumps([data, catalogs], sort_keys=True).encode()).hexdigest(),
                    'fdp.floren.app/generation': str(generation),
                    **({'fdp.floren.app/source-catalogs': source_catalogs} if source_catalogs else {})}},
                'spec': {'serviceAccountName': 'fdp-execution', 'automountServiceAccountToken': False,
                    # Workers drain before they stop: their running queries may take this long.
                    **({'terminationGracePeriodSeconds': spec.trino.workers.drain_seconds + 30} if role == 'worker' else {}),
                    'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                        'fsGroup': 1000, 'seccompProfile': {'type': 'RuntimeDefault'}},
                    'containers': [container, *tls_containers],
                    **({'initContainers': init_containers} if init_containers else {}), 'volumes': volumes}}}}, 'apps/v1'))
    # Layer-local artifacts: one volume per layer, never mounted outside it. The holder pod
    # keeps it attached so the control plane can prepare and read files through pod exec.
    artifact_labels = {**labels, LAYER: layer_id, COMPONENT: 'artifacts'}
    result.append(obj('PersistentVolumeClaim', ARTIFACT_CLAIM, {'spec': {
        'accessModes': [spec.storage.artifact_access_mode],
        **({'storageClassName': spec.storage.artifact_storage_class} if spec.storage.artifact_storage_class else {}),
        'resources': {'requests': {'storage': spec.storage.artifact_size}}}}))
    result[-1]['metadata']['annotations'] = {'fdp.floren.app/retention': 'Retain'}
    result.append(obj('Deployment', ARTIFACT_HOLDER, {'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'},
        'progressDeadlineSeconds': 600, 'selector': {'matchLabels': artifact_labels}, 'template': {
            'metadata': {'labels': artifact_labels},
            'spec': {'serviceAccountName': 'fdp-execution', 'automountServiceAccountToken': False,
                'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000,
                                    'seccompProfile': {'type': 'RuntimeDefault'}},
                'containers': [{'name': 'holder', 'image': config.artifact_image, 'command': ['sleep', 'infinity'],
                    'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                    'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '200m', 'memory': '128Mi'}},
                    'volumeMounts': [{'name': 'artifacts', 'mountPath': ARTIFACT_ROOT}]}],
                'volumes': [{'name': 'artifacts', 'persistentVolumeClaim': {'claimName': ARTIFACT_CLAIM}}]}}}}, 'apps/v1'))
    if config.trust_file:
        # Public certificates only: executions mount this; no Secret leaves the platform namespace.
        result.append(obj('ConfigMap', TRUST_CONFIGMAP, {'data': {'ca-bundle.pem': public_trust_pem(config.trust_file)}}))
    result.append(obj('Service', 'fdp-trino', {'spec': {
        'selector': {**trino, 'fdp.floren.app/trino-role': 'coordinator'},
        'ports': [{'name': 'https' if tls else 'http', 'port': 8443 if tls else 8080,
                   'targetPort': 'https' if tls else 'http'}]}}))
    if config.image_pull_secret:  # helpers run the platform image from a private registry
        for document in result:
            if document['kind'] in ('Deployment', 'StatefulSet'):
                document['spec']['template']['spec']['imagePullSecrets'] = [{'name': config.image_pull_secret}]
    return result
