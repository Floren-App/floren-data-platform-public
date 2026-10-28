"""The editor and its compute jobs see project files, never the volume root."""
import subprocess
from types import SimpleNamespace

import pytest
from kubernetes import client as k8s

from app.orchestrator.base import ExecutionSpec, prepare_home_command
from app.orchestrator.docker import DockerOrchestrator
from .test_layer_workspaces import orchestrator, spec


def prepare(root):
    return subprocess.run(prepare_home_command(str(root)), capture_output=True, text=True)


def test_home_initialization_is_idempotent_and_leaves_system_folders_alone(tmp_path):
    recovery = tmp_path / 'lost+found'
    recovery.mkdir(mode=0o700)
    (recovery / 'recovered').write_text('keep')
    assert prepare(tmp_path).returncode == 0
    project = tmp_path / 'workspace'
    assert project.stat().st_mode & 0o777 == 0o700
    (project / 'fdp.json').write_text('{}')
    assert prepare(tmp_path).returncode == 0
    assert (project / 'fdp.json').read_text() == '{}'
    assert (recovery / 'recovered').read_text() == 'keep'
    assert recovery.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('legacy', ['fdp.json', '.git', 'workspace', 'dangling'])
def test_home_initialization_refuses_legacy_data_without_moving_or_deleting_it(tmp_path, legacy):
    entry = tmp_path / legacy
    if legacy == 'workspace':
        entry.mkdir()
        (entry / 'keep').write_text('existing user files')
    elif legacy == 'dangling':
        entry.symlink_to(tmp_path / 'missing')
    else:
        entry.write_text('existing user files')
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    result = prepare(tmp_path)
    assert result.returncode != 0 and 'old layout' in result.stderr
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before


@pytest.mark.parametrize('linked', ['workspace', '.fdp-home-layout-v1'])
def test_home_initialization_refuses_symlinks(tmp_path, linked):
    (tmp_path / linked).symlink_to('/tmp')
    assert prepare(tmp_path).returncode != 0


@pytest.mark.parametrize('port', [None, 8080])
def test_kubernetes_workspace_editors_and_jobs_mount_only_the_initialized_subfolder(port):
    orch = orchestrator()
    execution = ExecutionSpec(name='workspace-execution', image='runtime:1', cpu=1, memory_gb=1,
                              home_volume='private-home', port=port, secret_env={'TOKEN': 'private'})
    orch.start(execution)
    pod = orch.core.objects[('fdp-runtime', 'pod', execution.name)]
    main = pod.spec.containers[0]
    home = next(m for m in main.volume_mounts if m.name == 'home')
    assert (home.mount_path, home.sub_path, main.working_dir) == ('/workspace', 'workspace', '/workspace')
    init, = pod.spec.init_containers
    assert init.command == prepare_home_command('/fdp-home-volume')
    assert init.volume_mounts[0].sub_path is None
    assert init.env is None and init.env_from is None
    assert init.security_context.run_as_non_root is True
    assert init.security_context.run_as_user == 1000
    assert init.security_context.allow_privilege_escalation is False


def test_published_workloads_do_not_initialize_or_mount_workspace_storage():
    orch = orchestrator()
    orch.start(ExecutionSpec(name='published', image='runtime:1', cpu=1, memory_gb=1))
    pod = orch.core.objects[('fdp-runtime', 'pod', 'published')]
    assert not pod.spec.init_containers
    assert not pod.spec.containers[0].volume_mounts


def test_initialization_failure_is_visible_while_the_editor_waits():
    orch = orchestrator()
    orch.core.read_namespaced_pod = lambda *_: k8s.V1Pod(status=k8s.V1PodStatus(
        phase='Pending', init_container_statuses=[k8s.V1ContainerStatus(
            name='prepare-home', image='editor:1', image_id='test', ready=False, restart_count=0,
            state=k8s.V1ContainerState(terminated=k8s.V1ContainerStateTerminated(exit_code=1)))]))
    assert 'workspace storage initialization failed' in orch.waiting_reason('workspace')


def test_docker_mounts_same_subfolder_after_a_credential_free_initializer():
    calls = []
    def run(image, **kwargs):
        calls.append((image, kwargs))
        return SimpleNamespace(name='editor')
    orch = object.__new__(DockerOrchestrator)
    orch.client = SimpleNamespace(containers=SimpleNamespace(get=lambda _: SimpleNamespace(remove=lambda **_: None), run=run))
    orch._workload_network = lambda *_: 'workspace-network'
    orch.start(spec())
    (_, init), (_, main) = calls
    assert init['entrypoint'] == prepare_home_command('/workspace')
    assert init['network_mode'] == 'none' and init['user'] == '1000:1000'
    assert 'environment' not in init
    mount, = main['mounts']
    assert mount['Target'] == '/workspace' and mount['VolumeOptions']['Subpath'] == 'workspace'
    assert 'volumes' not in main
    assert main['environment']['TOKEN'] == 'x'
