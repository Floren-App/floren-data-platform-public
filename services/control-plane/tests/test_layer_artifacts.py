"""Jobs and apps of a managed layer keep their files on that layer's own artifact volume."""
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from app import artifacts, layers
from app.config import get_settings
from app.db import SessionLocal
from app.models import ComputePool, GitRepository, LayerOperation, Workload, Run
from app.orchestrator.base import Placement, VolumeMount, ExecutionSpec
from app.orchestrator.fake import FakeOrchestrator
from app.orchestrator.kubernetes import artifact_path
from app.workspace_files import console, outputs, read_output
from .conftest import as_
from .test_layer_infrastructure import managed
from .test_layer_workspaces import Core, orchestrator
from .test_deploying import repo  # noqa: F401

NS = 'dc-x-layer-' + layers.NON_PROD


@pytest.fixture
def layer(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'layer_namespace_prefix', 'dc-x-layer')
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    assert client.put(path, headers=as_('admin'), json={'expected_generation': 0, 'spec': managed()}).status_code == 202

    def phase(value):
        with SessionLocal() as db:
            db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layers.NON_PROD)).phase = value
            db.commit()
    return phase


def job(volume):
    return ExecutionSpec(name='run-1', image='python:1', cpu=1, memory_gb=1,
                        mounts=[VolumeMount(volume, '/fdp/run', 'runs/r1'), VolumeMount(volume, '/fdp/envs', 'envs/p1')])


def test_jobs_wait_for_artifact_storage_then_use_only_the_layer_volume(layer):
    orch = FakeOrchestrator()
    layer('InfrastructureReady')  # workspaces may start; jobs need artifact storage
    with SessionLocal() as db, pytest.raises(HTTPException) as error:
        artifacts.place(db, job(get_settings().data_volume), layers.NON_PROD, orch)
    assert error.value.status_code == 409
    layer('Ready')
    with SessionLocal() as db:
        spec = artifacts.place(db, job(get_settings().data_volume), layers.NON_PROD, orch)
    assert spec.placement.namespace == NS
    assert {m.volume for m in spec.mounts} == {'fdp-artifacts'}
    assert orch.artifact_dirs[NS] == {'runs/r1', 'envs/p1'}
    with SessionLocal() as db, pytest.raises(ValueError):
        artifacts.place(db, job('someone-elses-volume'), layers.NON_PROD, orch)


def test_unmanaged_layer_keeps_the_shared_data_volume(client):
    orch = FakeOrchestrator()
    with SessionLocal() as db:
        spec = artifacts.place(db, job(get_settings().data_volume), layers.PROD, orch)
    assert spec.placement is None and {m.volume for m in spec.mounts} == {get_settings().data_volume}
    assert orch.artifact_dirs == {}


def test_deployed_run_console_and_outputs_come_from_its_layer(layer):
    layer('Ready')
    orch = FakeOrchestrator()
    orch.write_artifact(NS, 'runs/r1/console.log', b'line 1\nline 2\n')
    orch.write_artifact(NS, 'runs/r1/outputs/report.csv', b'a,b\n')
    orch.write_artifact('dc-x-layer-' + layers.PROD, 'runs/r1/outputs/other.csv', b'never visible')
    with SessionLocal() as db:
        pool = db.scalar(select(ComputePool))
        repo = GitRepository(name='layer-artifacts', url='https://example.test/repo.git')
        db.add(repo)
        db.flush()
        db.add(Workload(id='p1', slug='p1', title='P1', repo_id=repo.id, branch='main', program='job',
                           kind='script', runtime='python', owner='ana', warehouse=pool.name))
        db.flush()
        run = Run(id='r1', owner='ana', workload_id='p1', environment_id=layers.NON_PROD, kind='job', pool_id=pool.id,
                  entrypoint='main.py', status='succeeded')
        db.add(run)
        db.flush()
        assert console(orch, None, run, 1) == 'line 2\n'
        assert outputs(orch, None, run) == [('report.csv', 4)]
        assert read_output(orch, None, run, 'report.csv', 100) == b'a,b\n'
        assert read_output(orch, None, run, '../../etc/passwd', 100) is None


def test_artifact_paths_cannot_leave_the_layer_volume():
    assert artifact_path('runs/r1/console.log') == '/artifacts/runs/r1/console.log'
    for bad in ('', '/etc/passwd', '../x', 'runs/../../x'):
        with pytest.raises(ValueError):
            artifact_path(bad)


def test_kubernetes_reads_artifacts_through_the_running_holder_only(monkeypatch):
    orch = orchestrator()
    holder = lambda name, phase, deleting=None: SimpleNamespace(
        metadata=SimpleNamespace(name=name, deletion_timestamp=deleting), status=SimpleNamespace(phase=phase))
    pods = [holder('old', 'Running', deleting='now'), holder('new', 'Running')]
    orch.core.list_namespaced_pod = lambda namespace, label_selector: SimpleNamespace(items=pods)
    calls = []
    monkeypatch.setattr(orch, '_exec', lambda ref, cmd: calls.append((ref, cmd)) or (0, 'aGk='))
    assert orch.read_artifact(NS, 'runs/r1/console.log', 10) == b'hi'
    assert calls[-1][0] == NS + '/new' and '/artifacts/runs/r1/console.log' in calls[-1][1][-1]
    orch.prepare_artifacts(NS, ['runs/r1'])
    assert calls[-1][1][-1] == '/artifacts/runs/r1'
    pods[:] = []
    monkeypatch.setattr('time.sleep', lambda seconds: pods.append(holder('replacement', 'Running')))  # holder comes back
    assert orch.read_artifact(NS, 'runs/r1/console.log', 10) == b'hi'
    assert calls[-1][0] == NS + '/replacement'
    pods[:] = []
    monkeypatch.setattr('time.sleep', lambda seconds: None)
    monkeypatch.setattr('time.monotonic', iter([0, 0, 31]).__next__)  # never comes back within the wait
    with pytest.raises(RuntimeError):
        orch.read_artifact(NS, 'runs/r1/console.log', 10)
    # Executions in a layer may mount its artifact volume, never another volume.
    placed = Placement(NS)
    orch.start(ExecutionSpec(name='run-1', image='x', cpu=1, memory_gb=1, placement=placed,
                            mounts=[VolumeMount('fdp-artifacts', '/fdp/run', 'runs/r1')]))
    with pytest.raises(ValueError):
        orch.start(ExecutionSpec(name='run-2', image='x', cpu=1, memory_gb=1, placement=placed,
                                mounts=[VolumeMount('fdp-data', '/fdp/run', 'runs/r2')]))


def test_without_shared_volume_jobs_and_apps_need_a_managed_layer(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'shared_data_volume', False)
    orch = FakeOrchestrator()
    with SessionLocal() as db, pytest.raises(HTTPException) as error:
        artifacts.place(db, job(get_settings().data_volume), layers.PROD, orch)
    assert error.value.status_code == 409 and error.value.detail['code'] == 'ManagedLayerRequired'
    # Workspace-style specs (no shared mounts) still run in unmanaged layers.
    with SessionLocal() as db:
        plain = ExecutionSpec(name='ws-1', image='editor:1', cpu=1, memory_gb=1, home_volume='home-ana')
        assert artifacts.place(db, plain, layers.PROD, orch).placement is None


def test_app_deploy_into_an_unready_layer_fails_with_a_clear_reason(client, repo, layer):  # noqa: F811
    from .test_deploying import deploy
    layer('InfrastructureReady')  # apps need the fully Ready layer
    pub = deploy(client, repo, program='sales-dashboard', slug='dash').json()
    assert pub['status'] != 'live'
    assert 'not ready' in (pub.get('last_error') or json.dumps(pub)).lower()


def test_run_api_reports_layer_quota_instead_of_a_server_error(client, repo, layer, monkeypatch):  # noqa: F811
    from app import deps
    from app.orchestrator.base import CapacityError
    from .test_deploying import deploy
    layer('Ready')
    pub = deploy(client, repo, user='admin', program='explore', slug='explore').json()
    orch = deps.get_orchestrator()
    monkeypatch.setattr(orch, 'start', lambda spec: (_ for _ in ()).throw(CapacityError("This layer's resource quota is used up")))
    r = client.post(f"/v1/workloads/{pub['id']}/runs", headers=as_('admin'), json={'args': {}})
    assert r.status_code == 409 and r.json()['detail']['code'] == 'LayerQuotaExceeded'
    assert 'quota' in r.json()['detail']['message']
