"""Workspaces of a managed layer run, and keep their volume, inside that layer's namespace."""
from types import SimpleNamespace

from kubernetes.client.exceptions import ApiException
import pytest
from sqlalchemy import select

from app import deps, layers
from app.config import get_settings
from app.db import SessionLocal
from app.models import LayerOperation
from app.orchestrator.base import Placement, VolumeMount, ExecutionSpec
from app.orchestrator.kubernetes import KubernetesOrchestrator
from .conftest import as_
from .test_layer_infrastructure import managed


class Core:
    """Records CoreV1Api calls; objects are keyed by (namespace, kind, name)."""
    def __init__(self):
        self.objects, self.calls = {}, []

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.calls.append((method, args))
            kind = method.split('namespaced_', 1)[-1]
            if method.startswith('create_namespaced_'):
                namespace, body = args
                self.objects[(namespace, kind, body.metadata.name)] = body
            elif method.startswith(('read_namespaced_', 'delete_namespaced_')):
                name, namespace = args[0], args[1]
                if (namespace, kind, name) not in self.objects:
                    raise ApiException(status=404)
                found = (self.objects.pop if method.startswith('delete') else self.objects.get)((namespace, kind, name))
                return SimpleNamespace(metadata=found.metadata, status=SimpleNamespace(phase='Running')) if kind == 'pod' else found
            elif method == 'list_namespaced_pod':
                namespace = args[0]
                return SimpleNamespace(items=[SimpleNamespace(metadata=b.metadata, status=SimpleNamespace(phase='Running'))
                                              for (ns, kind, _), b in self.objects.items() if ns == namespace and kind == 'pod'])
        return call


def orchestrator(namespaces=('dc-x-layer-1a0000000002',)):
    settings = SimpleNamespace(k8s_namespace='fdp-runtime', k8s_storage_class='nfs-rwx')
    return KubernetesOrchestrator(settings, core=Core(), net=object(), layer_namespaces=lambda: list(namespaces))


def spec(**extra):
    return ExecutionSpec(name='ws-abc', image='editor:1', cpu=0.5, memory_gb=1, port=8080, home_volume='home-ana',
                        secret_env={'TOKEN': 'x'}, **extra)


def test_layer_workspace_runs_with_its_own_volume_and_namespaced_ref():
    orch = orchestrator()
    layer_ns = 'dc-x-layer-1a0000000002'
    handle = orch.start(spec(placement=Placement(layer_ns, storage_class='fast', volume_size='5Gi', access_mode='ReadWriteOnce')))
    assert handle.ref == layer_ns + '/ws-abc'
    assert handle.upstream_url == 'http://ws-abc.%s.svc:8080' % layer_ns
    objects = orch.core.objects
    assert {(ns, kind) for ns, kind, _ in objects} == {(layer_ns, k) for k in
        ('persistent_volume_claim', 'secret', 'pod', 'service')}
    pvc = objects[(layer_ns, 'persistent_volume_claim', 'home-ana')]
    assert (pvc.spec.storage_class_name, pvc.spec.access_modes, pvc.spec.resources.requests) == ('fast', ['ReadWriteOnce'], {'storage': '5Gi'})
    pod = objects[(layer_ns, 'pod', 'ws-abc')]
    assert pod.spec.service_account_name == 'fdp-execution' and pod.spec.automount_service_account_token is False
    assert orch.status(handle.ref) == 'running'
    assert [item['ref'] for item in orch.inventory()] == [handle.ref]
    orch.stop(handle.ref)
    assert orch.status(handle.ref) == 'missing'
    # The private volume survives the stop, still in the layer.
    assert (layer_ns, 'persistent_volume_claim', 'home-ana') in objects


def test_shared_namespace_behaviour_is_unchanged_and_shared_mounts_never_cross_into_layers():
    orch = orchestrator()
    handle = orch.start(spec())
    assert handle.ref == 'ws-abc' and handle.upstream_url == 'http://ws-abc.fdp-runtime.svc:8080'
    assert orch.core.objects[('fdp-runtime', 'persistent_volume_claim', 'home-ana')].spec.storage_class_name == 'nfs-rwx'
    with pytest.raises(ValueError):
        orch.start(spec(placement=Placement('dc-x-layer-1a0000000002'), mounts=[VolumeMount('fdp-data', '/out')]))


def test_workspace_starts_in_layer_namespace_once_infrastructure_is_ready(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'layer_namespace_prefix', 'dc-x-layer')
    spec_body = {**managed(), 'storage': {'workspace_storage_class': 'fast', 'workspace_size': '5Gi'}}
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    assert client.put(path, headers=as_('admin'), json={'expected_generation': 0, 'spec': spec_body}).status_code == 202
    ws = client.post('/v1/workspaces', headers=as_('admin'), json={'name': 'Layered', 'editor': 'vscode', 'layer_id': layers.NON_PROD}).json()
    assert client.post('/v1/workspaces/' + ws['id'] + '/start', headers=as_('admin')).status_code == 409
    with SessionLocal() as db:
        db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layers.NON_PROD)).phase = 'InfrastructureReady'
        db.commit()
    started = client.post('/v1/workspaces/' + ws['id'] + '/start', headers=as_('admin'))
    assert started.status_code == 200, started.text
    placed = deps.get_orchestrator().executions['ws-' + ws['id']].placement
    assert placed == Placement('dc-x-layer-' + layers.NON_PROD, storage_class='fast', volume_size='5Gi')
    # Jobs need layer-local artifacts, which are not reported Ready yet.
    with SessionLocal() as db:
        from app.layer_infrastructure import require_ready
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            require_ready(db, layers.NON_PROD)
        require_ready(db, layers.NON_PROD, 'workspace')
        # An empty binding means Non-Prod and is admitted the same way, never skipped.
        with pytest.raises(HTTPException):
            require_ready(db, None)


class QuotaCore(Core):
    """Kubernetes refuses the pod for the namespace's ResourceQuota."""
    def create_namespaced_pod(self, namespace, body):
        exc = ApiException(status=403, reason='Forbidden')
        exc.body = '{"message":"pods \\"ws-abc\\" is forbidden: exceeded quota: layer-budget"}'
        raise exc


def test_quota_refusal_becomes_a_clear_capacity_error_and_cleans_up():
    from app.orchestrator.base import CapacityError
    orch = orchestrator()
    orch.core = QuotaCore()
    layer_ns = 'dc-x-layer-1a0000000002'
    with pytest.raises(CapacityError) as error:
        orch.start(spec(placement=Placement(layer_ns)))
    assert 'quota' in str(error.value) and '0.5 vCPU' in str(error.value)
    assert (layer_ns, 'secret', 'ws-abc-env') not in orch.core.objects  # no orphaned credentials
