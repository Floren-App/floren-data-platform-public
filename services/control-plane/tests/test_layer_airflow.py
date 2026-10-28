"""Standalone Airflow runs inside its managed layer, with metadata on that layer's own volume."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import airflow_history, deps, layers
from app.artifacts import LayerArtifacts
from app.config import get_settings
from app.db import SessionLocal
from app.models import LayerOperation
from .conftest import as_
from .test_airflow_history import metadata
from .test_layer_infrastructure import managed
from .test_platform import platform_repo, push  # noqa: F401
from .test_deploying import deploy, repo  # noqa: F401

NS = 'dc-x-layer-' + layers.NON_PROD


@pytest.fixture
def ready_layer(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'layer_namespace_prefix', 'dc-x-layer')
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    assert client.put(path, headers=as_('admin'), json={'expected_generation': 0, 'spec': managed()}).status_code == 202
    with SessionLocal() as db:
        db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layers.NON_PROD)).phase = 'Ready'
        db.commit()


def test_standalone_airflow_is_placed_in_its_layer_with_metadata_on_the_layer_volume(client, platform_repo, ready_layer):  # noqa: F811
    pub = deploy(client, platform_repo, program='pipelines', slug='pipelines').json()
    assert pub['status'] == 'live', pub
    orch = deps.get_orchestrator()
    spec = orch.executions[f"pub-{pub['id']}-{pub['deployed_sha'][:7]}"]
    assert spec.placement.namespace == NS
    assert {m.volume for m in spec.mounts} == {'fdp-artifacts'}
    assert {m.target for m in spec.mounts} >= {'/fdp/airflow'}
    assert f"airflow/{pub['id']}" in orch.artifact_dirs[NS]


def distributed(w):
    cfg = json.loads((w / 'fdp.json').read_text())
    cfg['programs']['pipelines']['airflow_settings'] = {'mode': 'distributed', 'workers': 2}
    (w / 'fdp.json').write_text(json.dumps(cfg))


def test_distributed_airflow_runs_entirely_inside_its_layer_with_its_own_metadata_database(
        client, platform_repo, ready_layer):  # noqa: F811
    push(platform_repo['work'], 'Distributed Airflow', distributed)
    orch = deps.get_orchestrator()
    pub = deploy(client, platform_repo, program='pipelines', slug='pipelines').json()
    assert pub['status'] == 'live', pub
    prefix = f"pub-{pub['id']}"
    mine = {name: spec for name, spec in orch.executions.items() if name.startswith(prefix)}
    assert set(mine) == {f'{prefix}-broker', f'{prefix}-metadata', *(f"{prefix}-{pub['deployed_sha'][:7]}-{role}"
                         for role in ('web', 'scheduler', 'worker-1', 'worker-2'))}
    assert all(spec.placement and spec.placement.namespace == NS for spec in mine.values())
    assert all(m.volume == 'fdp-artifacts' for spec in mine.values() for m in spec.mounts)
    metadata = mine[f'{prefix}-metadata']
    assert [(m.target, m.subpath) for m in metadata.mounts] == [('/var/lib/postgresql/data', f"airflow/{pub['id']}/metadata")]
    assert metadata.labels['fdp.floren.app/role'] == 'airflow-metadata' and 'POSTGRES_PASSWORD' in metadata.secret_env
    broker = mine[f'{prefix}-broker']
    password = broker.secret_env['BROKER_PASSWORD']
    assert '--requirepass' in broker.command[-1] and password not in ' '.join(broker.command)
    web = mine[f"{prefix}-{pub['deployed_sha'][:7]}-web"]
    assert web.secret_env['AIRFLOW__CELERY__BROKER_URL'].startswith('redis://:') and password in web.secret_env['AIRFLOW__CELERY__BROKER_URL']
    assert f'@{prefix}-metadata:5432/airflow' in web.secret_env['AIRFLOW__DATABASE__SQL_ALCHEMY_CONN']
    assert 'AIRFLOW__CELERY__BROKER_URL' not in web.env  # credentials never in plain pod env
    # The components started only after the database accepted logins.
    assert orch.execs and orch.execs[0][0] == f'{NS}/{prefix}-metadata' and orch.execs[0][1][0] == 'pg_isready'

    # A redeploy replaces the versioned components but keeps the broker and the database running.
    broker_before, metadata_before = orch.executions[f'{prefix}-broker'], orch.executions[f'{prefix}-metadata']
    def change(w):
        dag = sorted((w / 'airflow').rglob('*.py'))[0]
        dag.write_text(dag.read_text() + '# changed\n')
    push(platform_repo['work'], 'Change DAGs', change)
    from .test_deploying import reconcile
    reconcile()
    with SessionLocal() as db:
        from app.models import Workload
        current = db.get(Workload, pub['id'])
        assert current.deployed_sha != pub['deployed_sha'] and current.status == 'live'
        assert f'{NS}/{prefix}-broker' in current.execution_refs and f'{NS}/{prefix}-metadata' in current.execution_refs
    assert orch.executions[f'{prefix}-broker'] is broker_before and orch.executions[f'{prefix}-metadata'] is metadata_before
    assert not any(name.startswith(f"{prefix}-{pub['deployed_sha'][:7]}") for name in orch.executions)


def test_distributed_airflow_waits_for_its_database_and_fails_clearly_when_it_never_starts(
        client, platform_repo, ready_layer, monkeypatch):  # noqa: F811
    push(platform_repo['work'], 'Distributed Airflow', distributed)
    orch = deps.get_orchestrator()
    monkeypatch.setattr(get_settings(), 'app_start_timeout_seconds', 0.2)
    orch.exec_result = lambda ref, cmd: (2, '')
    pub = deploy(client, platform_repo, program='pipelines', slug='pipelines').json()
    assert pub['status'] != 'live' and 'metadata database did not start' in json.dumps(pub)
    assert not any('-web' in name or '-worker' in name for name in orch.executions)


def test_shared_namespace_distributed_airflow_keeps_its_open_broker(tmp_path):
    from app.executions import airflow_broker_spec
    spec = airflow_broker_spec(get_settings(), 'p1')
    assert spec.command[0] == 'valkey-server' and not spec.secret_env  # unchanged for existing deployments


def test_layer_policies_open_airflow_backends_only_to_airflow_components():
    from app.layer_manifests import manifests
    from .test_layer_controller import WORKERS, CONFIG
    from app.layer_infrastructure import InfrastructureSpec
    docs = {d['metadata']['name']: d for d in manifests(layers.NON_PROD, 1, InfrastructureSpec.model_validate(managed()), CONFIG, workers=WORKERS)
            if d['kind'] == 'NetworkPolicy'}
    backends = docs['airflow-backends']['spec']
    assert backends['policyTypes'] == ['Ingress']
    assert backends['podSelector']['matchExpressions'][0]['values'] == ['broker', 'airflow-metadata']
    assert backends['ingress'][0]['from'] == [{'podSelector': {'matchExpressions': [
        {'key': 'fdp.floren.app/airflow-role', 'operator': 'Exists'}]}}]
    assert {p['port'] for p in backends['ingress'][0]['ports']} == {6379, 5432}
    assert docs['airflow-components']['spec']['policyTypes'] == ['Egress']


def test_history_of_a_distributed_layer_deployment_is_read_inside_its_metadata_pod():
    seen = []

    def execution_exec(ref, cmd):
        seen.append((ref, cmd))
        return 0, json.dumps({'runs': [{'dag_id': 'd', 'run_id': 'r1', 'state': 'success',
            'logical_date': '2026-10-21T10:00:00.000000+00:00', 'start_date': None, 'end_date': None}], 'dags': ['d']})
    orch = SimpleNamespace(execution_exec=execution_exec)
    pub = SimpleNamespace(id='p1', program_def={'airflow_settings': {'mode': 'distributed'}})
    history = airflow_history.recent_runs(pub, store=None, limit=3, dag_id="d' OR 1=1", artifacts=LayerArtifacts(orch, NS))
    assert history['runs'][0]['logical_date'].tzinfo is not None and history['dags'] == ['d']
    ref, cmd = seen[0]
    assert ref == f'{NS}/pub-p1-metadata'
    assert cmd[:2] == ['sh', '-c'] and cmd[2] == airflow_history.LAYER_PG_QUERY and cmd[3:] == ['history', "d' OR 1=1", '3']


def run_query(db_file, limit, dag_id):
    result = subprocess.run([sys.executable, '-c', airflow_history.LAYER_QUERY, str(db_file), str(limit), dag_id],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_layer_history_query_is_bounded_filtered_read_only_and_never_returns_configuration(tmp_path):
    engine, _ = metadata(tmp_path, 'p1')
    engine.dispose()
    db_file = tmp_path / 'airflow' / 'p1' / 'airflow.db'
    body = run_query(db_file, 3, 'cards')
    assert [r['run_id'] for r in body['runs']] == ['run-1', 'run-3', 'run-5']
    assert body['dags'] == ['cards', 'prices'] and 'SECRET' not in json.dumps(body)
    assert run_query(db_file, 5, "cards' OR 1=1 --")['runs'] == []
    missing = subprocess.run([sys.executable, '-c', airflow_history.LAYER_QUERY, str(tmp_path / 'none.db'), '5', ''],
                             capture_output=True, text=True)
    assert missing.returncode != 0 and not (tmp_path / 'none.db').exists()  # read-only: never creates a database


def test_history_for_a_layer_workload_runs_inside_its_holder(tmp_path):
    engine, _ = metadata(tmp_path, 'p1')
    engine.dispose()
    seen = []

    def holder_exec(namespace, cmd):
        seen.append((namespace, cmd[:2], cmd[3:]))
        local = [part.replace('/artifacts/', str(tmp_path) + '/') if part.startswith('/artifacts/') else part for part in cmd]
        result = subprocess.run([sys.executable if local[0] == 'python' else local[0], *local[1:]], capture_output=True, text=True)
        return result.returncode, result.stdout

    orch = SimpleNamespace(holder_exec=holder_exec)
    pub = SimpleNamespace(id='p1', program_def={})
    history = airflow_history.recent_runs(pub, store=None, limit=2, artifacts=LayerArtifacts(orch, NS))
    assert [r['run_id'] for r in history['runs']] == ['run-0', 'run-1']
    assert history['runs'][0]['end_date'].tzinfo is not None
    assert seen == [(NS, ['python', '-c'], ['/artifacts/airflow/p1/airflow.db', '2', ''])]
