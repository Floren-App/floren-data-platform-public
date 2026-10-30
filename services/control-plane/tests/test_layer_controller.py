"""Controller recovery, ownership and network boundaries, without a live cluster."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from fastapi import HTTPException
from kubernetes.client.exceptions import ApiException
import pytest
from sqlalchemy import select

from app import layer_controller as controller, layer_infrastructure as infra, layers
from app.db import SessionLocal, engine
from app.layer_manifests import ControllerConfig, LAYER, OWNER, ProvisioningError, WorkerPlan, manifests
from app.models import LayerOperation, utcnow
from .conftest import as_
from .test_layer_infrastructure import managed

WORKERS = WorkerPlan('trino', 1, 4, 1)  # the test warehouse resolved by the controller
CONFIG = ControllerConfig('platform', 'installation-a', 'fdp-layer', 'fdp-runtime', artifact_image='fdp/control-plane:1')


class MemoryKubernetes:
    """API-shaped storage: resourceVersion, conflicts and delayed rollout status."""
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_on = None
        self.version = 0

    def call_api(self, path, method, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if self.fail_on:
            if self.fail_on(method, path):
                raise ApiException(status=503, reason='secret database password must never escape',
                                   http_resp=None)
        if method == 'GET':
            if path.endswith('/networkpolicies'):
                selector = dict(kwargs.get('query_params', [])).get('labelSelector', '')
                labels = dict(pair.split('=', 1) for pair in selector.split(',') if pair)
                return {'metadata': {}, 'items': [deepcopy(obj) for key, obj in self.objects.items()
                    if key.startswith(path + '/') and all(obj['metadata']['labels'].get(k) == v for k, v in labels.items())]}
            if path not in self.objects:
                raise ApiException(status=404)
            return deepcopy(self.objects[path])
        body = deepcopy(kwargs['body'])
        if method == 'POST':
            path += '/' + body['metadata']['name']
            if path in self.objects:
                raise ApiException(status=409)
            body['metadata']['generation'] = 1
        elif method == 'PATCH':
            assert dict(kwargs['query_params'])['force'] == 'true'
            assert kwargs['header_params']['Content-Type'] == 'application/apply-patch+yaml'
            old = self.objects[path]
            if body['metadata']['resourceVersion'] != old['metadata']['resourceVersion']:
                raise ApiException(status=409)
            body['metadata']['generation'] = old['metadata']['generation'] + int(body.get('spec') != old.get('spec'))
            if 'status' in old:
                body['status'] = deepcopy(old['status'])
        else:
            raise AssertionError('controller must never delete resources')
        self.version += 1
        body['metadata']['resourceVersion'] = str(self.version)
        self.objects[path] = body
        return deepcopy(body)

    def rollouts_ready(self):
        for obj in self.objects.values():
            if obj['kind'] == 'Deployment':
                replicas = obj['spec']['replicas']
                obj['status'] = {'observedGeneration': obj['metadata']['generation'],
                    'replicas': replicas, 'updatedReplicas': replicas, 'readyReplicas': replicas}


def queue(client, spec=None):
    result = client.put('/v1/admin/layers/' + layers.NON_PROD + '/infrastructure', headers=as_('admin'),
                        json={'expected_generation': 0, 'spec': spec or managed()})
    assert result.status_code == 202, result.text
    return result.json()['operation']['id']


def due(operation_id, **changes):
    with SessionLocal() as db:
        operation = db.get(LayerOperation, operation_id)
        operation.next_attempt_at = utcnow() - timedelta(seconds=1)
        for key, value in changes.items():
            setattr(operation, key, value)
        db.commit()


def operation(operation_id):
    with SessionLocal() as db:
        return db.get(LayerOperation, operation_id)


def test_controller_converges_after_partial_failure_and_rechecks_drift(client):
    operation_id = queue(client)
    api = MemoryKubernetes()
    resources = controller.KubernetesResources(api)
    api.fail_on = lambda method, path: method == 'POST' and path.endswith('/deployments')
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).reason == 'KubernetesUnavailable'
    assert operation(operation_id).phase == 'Provisioning'
    assert 'password' not in operation(operation_id).message
    assert any(obj['kind'] == 'NetworkPolicy' for obj in api.objects.values())
    assert controller.claim_operation() is None  # honors retry delay
    api.fail_on = None
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).reason == 'WaitingForRollout'
    api.rollouts_ready()
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).phase == 'Ready'
    assert operation(operation_id).reason == 'Reconciled'
    # Trino and artifact storage are healthy: jobs and apps are admitted.
    with SessionLocal() as db:
        infra.require_ready(db, layers.NON_PROD)
    count = len(api.objects)
    policy = next(obj for obj in api.objects.values() if obj['kind'] == 'NetworkPolicy' and obj['metadata']['name'] == 'default-deny')
    policy['spec']['ingress'] = [{}]  # drift is restored without adopting foreign objects
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert len(api.objects) == count
    policy = next(obj for obj in api.objects.values() if obj['kind'] == 'NetworkPolicy' and obj['metadata']['name'] == 'default-deny')
    assert policy['spec']['ingress'] == []
    assert operation(operation_id).phase == 'Ready'


def test_a_ready_layer_keeps_admitting_work_while_the_same_generation_rolls_out(client):
    operation_id = queue(client)
    api = MemoryKubernetes()
    resources = controller.KubernetesResources(api)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).phase == 'Provisioning'  # first rollout: work is not admitted yet
    with SessionLocal() as db, pytest.raises(HTTPException):
        infra.require_ready(db, layers.NON_PROD)
    api.rollouts_ready()
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).phase == 'Ready'
    # A platform upgrade changes the helper image: the holder rolls, Trino keeps serving.
    upgraded = ControllerConfig(**{**CONFIG.__dict__, 'artifact_image': 'fdp/control-plane:2'})
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, upgraded)
    assert (operation(operation_id).phase, operation(operation_id).reason) == ('Ready', 'RollingUpdate')
    with SessionLocal() as db:
        infra.require_ready(db, layers.NON_PROD)
    api.rollouts_ready()
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, upgraded)  # still claimable, then settles
    assert (operation(operation_id).phase, operation(operation_id).reason) == ('Ready', 'Reconciled')


def test_claim_is_exclusive_and_crashed_controller_can_be_replaced(client):
    operation_id = queue(client)
    first = controller.claim_operation()
    assert first.operation_id == operation_id
    assert controller.claim_operation() is None
    due(operation_id, lease_expires_at=utcnow() - timedelta(seconds=1))
    replacement = controller.claim_operation()
    assert replacement.token != first.token
    with pytest.raises(controller.LostLease):
        controller.renew(first)
    with pytest.raises(controller.LostLease):
        controller.finish(first, 'Failed', 'OldController', 'Must not overwrite new status', 0)
    api = MemoryKubernetes()
    controller.reconcile(replacement, controller.KubernetesResources(api), CONFIG)
    assert operation(operation_id).reason == 'WaitingForRollout'


def test_layer_update_cannot_race_a_claim_and_supersedes_old_infrastructure(client):
    operation_id = queue(client)
    claim = controller.claim_operation()
    controller.finish(claim, 'InfrastructureReady', 'RuntimeIntegrationPending', 'Waiting for runtime integration', 0)
    claim = controller.claim_operation()
    spec = managed()
    spec['trino']['workers'] = {'warehouse': 'trino', 'min': 2, 'max': 2}
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    request = {'expected_generation': 1, 'spec': spec}
    assert client.put(path, headers=as_('admin'), json=request).status_code == 409
    controller.finish(claim, 'InfrastructureReady', 'RuntimeIntegrationPending', 'Waiting for runtime integration', 0)
    assert client.put(path, headers=as_('admin'), json=request).json()['generation'] == 2
    assert operation(operation_id).phase == 'Superseded'
    assert controller.claim_operation().generation == 2


def test_foreign_namespace_is_never_adopted_or_mutated(client):
    operation_id = queue(client)
    api = MemoryKubernetes()
    namespace = infra.namespace_for(layers.NON_PROD)
    path = '/api/v1/namespaces/' + namespace
    api.objects[path] = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {
        'name': namespace, 'labels': {OWNER: 'other-installation', LAYER: layers.NON_PROD}, 'resourceVersion': '1'}}
    original = deepcopy(api.objects)
    controller.reconcile(controller.claim_operation(), controller.KubernetesResources(api), CONFIG)
    assert operation(operation_id).phase == 'Failed'
    assert operation(operation_id).reason == 'OwnershipConflict'
    assert api.objects == original
    assert all(method == 'GET' for method, _, _ in api.calls)


@pytest.mark.parametrize('change,reason', [
    ({'storage': {'artifact_secret': {'name': 'artifacts', 'key': 'config'}}}, 'UnsupportedArtifacts'),
    ({'quota': {'cpu': 0.5}}, 'InsufficientQuota'),
    ({'rustfs': {'enabled': True, 'credentials_secret': 'rustfs-auth', 'image': 'rustfs/rustfs:1.0.2', 'size': '2Ti'}},
     'InsufficientQuota'),
    ({'trino': {'workers': {'warehouse': 'trino', 'min': 1, 'max': 40}}}, 'InsufficientQuota'),
])
def test_unsupported_or_impossible_desired_state_fails_before_mutation(client, change, reason):
    operation_id = queue(client, {**managed(), **change})
    api = MemoryKubernetes()
    controller.reconcile(controller.claim_operation(), controller.KubernetesResources(api), CONFIG)
    assert operation(operation_id).phase == 'Failed'
    assert operation(operation_id).reason == reason
    assert api.calls == []


def test_policies_scope_peers_to_correct_namespace_and_compute_has_no_api_credentials():
    spec = infra.InfrastructureSpec.model_validate({**managed(), 'trino': {'workers': {'warehouse': 'trino', 'min': 2, 'max': 2}}})
    docs = manifests(layers.NON_PROD, 7, spec, CONFIG, workers=WorkerPlan('trino', 1, 4, 2))
    assert docs[0]['kind'] == 'Namespace'
    assert docs[1]['metadata']['name'] == 'default-deny'
    namespace = docs[0]['metadata']['name']
    assert docs[0]['metadata']['labels']['pod-security.kubernetes.io/enforce'] == 'baseline'
    assert docs[0]['metadata']['labels']['pod-security.kubernetes.io/warn'] == 'restricted'
    assert all(d['metadata'].get('namespace', namespace) == namespace for d in docs)
    policies = {d['metadata']['name']: d['spec'] for d in docs if d['kind'] == 'NetworkPolicy'}
    assert policies['default-deny']['ingress'] == policies['default-deny']['egress'] == []
    dns_peer = policies['dns']['egress'][0]['to'][0]
    assert dns_peer['namespaceSelector']['matchLabels'] == {'kubernetes.io/metadata.name': 'kube-system'}
    assert dns_peer['podSelector']['matchLabels'] == {'k8s-app': 'kube-dns'}
    cp_peer = policies['trino-gateway']['ingress'][0]['from'][0]
    assert cp_peer['namespaceSelector']['matchLabels'] == {'kubernetes.io/metadata.name': 'platform'}
    assert cp_peer['podSelector']['matchLabels'] == {'app.kubernetes.io/name': 'fdp-control-plane'}
    internal_peer = policies['trino-internal']['egress'][0]['to'][0]
    assert 'namespaceSelector' not in internal_peer
    assert internal_peer['podSelector']['matchLabels'][LAYER] == layers.NON_PROD
    assert not any('ipBlock' in json.dumps(p) for p in policies.values())
    gateway = policies['gateway-ingress']
    assert gateway['podSelector']['matchLabels'] == {'fdp.floren.app/managed': 'true'} and 'egress' not in gateway
    assert gateway['ingress'][0]['from'][0]['namespaceSelector']['matchLabels'] == {'kubernetes.io/metadata.name': 'platform'}
    assert gateway['ingress'][0]['from'][0]['podSelector']['matchLabels'] == {'app.kubernetes.io/name': 'traefik'}
    # The control plane polls editor health directly; without this, workspaces never become ready.
    assert gateway['ingress'][0]['from'][1]['podSelector']['matchLabels'] == {'app.kubernetes.io/name': 'fdp-control-plane'}
    assert len(gateway['ingress'][0]['from']) == 2
    assert not any(d['kind'] in ('Secret', 'PersistentVolume') for d in docs)
    # The only claim is the layer's own retained artifact volume, held by a token-less pod.
    claims = [d for d in docs if d['kind'] == 'PersistentVolumeClaim']
    assert [c['metadata']['name'] for c in claims] == ['fdp-artifacts']
    assert claims[0]['metadata']['annotations']['fdp.floren.app/retention'] == 'Retain'
    holder = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'fdp-artifacts')
    pod = holder['spec']['template']['spec']
    assert pod['volumes'] == [{'name': 'artifacts', 'persistentVolumeClaim': {'claimName': 'fdp-artifacts'}}]
    assert pod['securityContext']['runAsUser'] == 1000 and pod['automountServiceAccountToken'] is False
    binding = next(d for d in docs if d['kind'] == 'RoleBinding')
    assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': 'fdp-control-plane', 'namespace': 'platform'}]
    deployments = [d for d in docs if d['kind'] == 'Deployment']
    assert [d['spec']['replicas'] for d in deployments] == [1, 2, 1]
    for deployment in deployments:
        pod = deployment['spec']['template']['spec']
        assert pod['automountServiceAccountToken'] is False
        assert pod['serviceAccountName'] == 'fdp-execution'
        assert pod['securityContext']['seccompProfile']['type'] == 'RuntimeDefault'
        assert all('nfs' not in v and 'hostPath' not in v for v in pod['volumes'])
    configs = [d for d in docs if d['kind'] == 'ConfigMap']
    assert all('fdp-control-plane.platform.svc' in d['data']['access-control.properties'] for d in configs)
    assert all(layers.NON_PROD in d['data']['node.properties'] for d in configs)


def test_readiness_rejects_old_rollouts_and_draining_replicas():
    deployment = {'metadata': {'generation': 2}, 'spec': {'replicas': 1},
                  'status': {'observedGeneration': 1, 'readyReplicas': 1, 'updatedReplicas': 1, 'replicas': 1}}
    assert not controller.deployment_ready(deployment)
    deployment['status']['observedGeneration'] = 2
    assert controller.deployment_ready(deployment)
    deployment['status']['replicas'] = 2
    assert not controller.deployment_ready(deployment)


def test_real_http_transport_serializes_apply_and_handles_api_errors(tmp_path):
    """Exercise the real SDK transport, not only the injected Python API double."""
    store = MemoryKubernetes()
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self):
            parsed = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.headers.get('Content-Length') else None
            seen.append((self.command, self.headers.get('Authorization'), self.headers.get('Content-Type'), parse_qs(parsed.query)))
            try:
                query = [(key, values[0]) for key, values in parse_qs(parsed.query).items()]
                result = store.call_api(parsed.path, self.command, body=body, query_params=query,
                                        header_params={'Content-Type': self.headers.get('Content-Type')})
                status = 200
            except ApiException as exc:
                status, result = exc.status, {'kind': 'Status', 'message': 'API failure'}
            payload = json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = respond

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = Path(tmp_path) / 'token'
    token.write_text('test-service-account-token\n')
    try:
        resources = controller.KubernetesResources(controller.InClusterApi(
            'http://127.0.0.1:%s' % server.server_port, token_file=token))
        namespace = manifests(layers.NON_PROD, 1, infra.InfrastructureSpec.model_validate(managed()), CONFIG, workers=WORKERS)[0]
        created = resources.apply(namespace)
        assert created['metadata']['name'] == namespace['metadata']['name']
        updated = resources.apply(namespace)
        assert updated['metadata']['resourceVersion'] != created['metadata']['resourceVersion']
        token.write_text('rotated-token')  # bound tokens rotate; each request rereads it
        with pytest.raises(ApiException) as error:
            resources.request('GET', '/api/v1/namespaces/missing')
        assert error.value.status == 404
        assert all(auth == 'Bearer test-service-account-token' for _, auth, _, _ in seen[:4])
        assert seen[4][1] == 'Bearer rotated-token'
        assert [method for method, _, _, _ in seen] == ['GET', 'POST', 'GET', 'PATCH', 'GET']
        assert seen[3][2] == 'application/apply-patch+yaml'
        assert seen[3][3]['fieldManager'] == ['fdp-layer-controller']
        assert seen[3][3]['force'][0] == 'true'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='real PostgreSQL row locks required')
def test_concurrent_postgres_controllers_only_one_claims_generation(client):
    operation_id = queue(client)
    barrier = threading.Barrier(2)

    def take_claim():
        barrier.wait(timeout=10)
        return controller.claim_operation()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: take_claim(), range(2)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].operation_id == operation_id



def test_layer_tokens_verify_only_against_their_own_layer_audience(client, monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    from app import sql_access
    from app.config import get_settings
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
    monkeypatch.setattr(get_settings(), 'trino_jwt_private_key', pem)
    jwks = client.get('/internal/trino/jwks').json()
    assert set(jwks['keys'][0]) >= {'kty', 'crv', 'x', 'y', 'kid'} and 'd' not in jwks['keys'][0]
    public = jwt.PyJWK(jwks['keys'][0]).key
    token = sql_access.layer_engine_token('p1', layers.NON_PROD)
    assert jwt.get_unverified_header(token)['kid'] == jwks['keys'][0]['kid']
    claims = jwt.decode(token, public, algorithms=['ES256'], audience='trino-layer-' + layers.NON_PROD, issuer='fdp-sql')
    assert claims['sub'] == 'p1'
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(token, public, algorithms=['ES256'], audience='trino-layer-' + layers.PROD)


def test_controller_generates_layer_node_secret_once_and_trino_uses_platform_jwks(client):
    queue(client)
    api = MemoryKubernetes()
    resources = controller.KubernetesResources(api)
    claim = controller.claim_operation()
    controller.reconcile(claim, resources, CONFIG)
    namespace = infra.namespace_for(layers.NON_PROD)
    path = '/api/v1/namespaces/%s/secrets/%s' % (namespace, 'fdp-trino-internal')
    first = api.objects[path]['stringData']['internal-secret']
    assert len(first) >= 48 and api.objects[path]['metadata']['labels'][LAYER] == layers.NON_PROD
    due(claim.operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert api.objects[path]['stringData']['internal-secret'] == first  # kept, never re-generated
    config = next(o for o in api.objects.values() if o['kind'] == 'ConfigMap')['data']['config.properties']
    assert 'jwt.key-file=http://fdp-control-plane.platform.svc:8000/internal/trino/jwks' in config
    assert 'required-audience=trino-layer-' + layers.NON_PROD in config
    access = next(o for o in api.objects.values() if o['kind'] == 'ConfigMap')['data']['access-control.properties']
    assert 'opa.policy.uri=http://fdp-control-plane.platform.svc:8000/internal/trino/layers/%s/allow' % layers.NON_PROD in access
    assert '/internal/trino/allow' not in access
    assert not any(o['kind'] == 'Secret' and o['metadata']['namespace'] != namespace for o in api.objects.values())


def test_layers_without_workspaces_enforce_pod_security_restricted():
    """Platform pods and programs meet "restricted"; only workspace editors start as root."""
    spec = infra.InfrastructureSpec.model_validate({**managed(), 'trino': {'workers': {'warehouse': 'trino', 'min': 1, 'max': 1}}})
    plan = WorkerPlan('trino', 1, 4, 1)
    labels = lambda **kw: manifests(layers.PROD, 1, spec, CONFIG, workers=plan, **kw)[0]['metadata']['labels']
    assert labels(workspaces=False)['pod-security.kubernetes.io/enforce'] == 'restricted'
    assert labels(workspaces=True)['pod-security.kubernetes.io/enforce'] == 'baseline'
    assert labels(workspaces=True)['pod-security.kubernetes.io/warn'] == 'restricted'


def test_claims_carry_the_layer_workspace_setting(client):
    from app.models import Resource
    with SessionLocal() as db:
        db.get(Resource, layers.NON_PROD).config = {**db.get(Resource, layers.NON_PROD).config, 'workspaces': False}
        db.commit()
    queue(client)
    claim = controller.claim_operation()
    assert claim.layer_id == layers.NON_PROD and claim.workspaces is False
