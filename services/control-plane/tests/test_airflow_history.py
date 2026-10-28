from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine, select

from app import airflow_history, deps
from app.config import get_settings
from app.db import SessionLocal
from app.models import Workload
from .conftest import as_
from .rbac_helpers import grant_group
from .test_catalog import register
from .test_deploying import repo  # noqa: F401


def metadata(tmp_path, pub_id):
    path = tmp_path / 'airflow' / pub_id / 'airflow.db'
    path.parent.mkdir(parents=True)
    engine = create_engine('sqlite:///' + str(path))
    table = Table('dag_run', MetaData(), Column('dag_id', String), Column('run_id', String),
        Column('state', String), Column('execution_date', DateTime), Column('start_date', DateTime),
        Column('end_date', DateTime), Column('conf', String))
    table.metadata.create_all(engine)
    now = datetime(2026, 10, 19, 12)
    with engine.begin() as conn:
        conn.execute(table.insert(), [dict(dag_id='cards' if i % 2 else 'prices', run_id=f'run-{i}',
            state='success', execution_date=now - timedelta(hours=i), start_date=now - timedelta(hours=i),
            end_date=now - timedelta(hours=i) + timedelta(minutes=10), conf='SECRET CONFIG') for i in range(8)])
    return engine, table


def test_native_history_is_bounded_filtered_and_does_not_return_run_configuration(client, repo, monkeypatch, tmp_path):
    pid, _ = register(client, repo, kind='airflow', slug='pipelines')
    monkeypatch.setattr(get_settings(), 'data_dir', str(tmp_path))
    engine, table = metadata(tmp_path, pid)
    endpoint = f'/v1/workloads/{pid}/orchestrator-runs'
    result = client.get(endpoint, headers=as_('ana'), params={'limit': 3, 'dag_id': 'cards'})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['dags'] == ['cards', 'prices']
    assert [r['run_id'] for r in body['runs']] == ['run-1', 'run-3', 'run-5']
    assert body['runs'][0]['end_date'].endswith('Z') or body['runs'][0]['end_date'].endswith('+00:00')
    assert 'SECRET' not in result.text and 'conf' not in body['runs'][0]
    assert client.get(endpoint, headers=as_('ana'), params={'limit': 26}).status_code == 422
    assert client.get(endpoint, headers=as_('ana'), params={'dag_id': "cards' OR 1=1 --"}).json()['runs'] == []
    with engine.connect() as conn:
        assert len(list(conn.execute(select(table)))) == 8
    engine.dispose()


def test_history_never_merges_workloads_and_checks_access_before_opening_metadata(client, repo, monkeypatch, tmp_path):
    test_id, test_rid = register(client, repo, kind='airflow', slug='test-pipelines')
    prod_id, _ = register(client, repo, kind='airflow', slug='prod-pipelines')
    monkeypatch.setattr(get_settings(), 'data_dir', str(tmp_path))
    test_engine, _ = metadata(tmp_path, test_id)
    for group in client.get('/v1/rbac/groups', headers=as_('admin')).json():
        if group['name'] == 'Deployed executors':
            client.delete(f"/v1/rbac/resources/workloads/assignments/{group['id']}", headers=as_('admin'))
    grant_group(client, test_rid, 'Test pipeline viewers', 'Executor', users=['bob'])
    assert client.get(f'/v1/workloads/{test_id}/orchestrator-runs', headers=as_('bob')).status_code == 200
    # The inaccessible Prod database does not even exist: authorization must win over I/O.
    assert client.get(f'/v1/workloads/{prod_id}/orchestrator-runs', headers=as_('bob')).status_code == 404
    assert not (tmp_path / 'airflow' / prod_id / 'airflow.db').exists()
    test_engine.dispose()


def test_unavailable_metadata_does_not_create_a_database_or_leak_driver_errors(client, repo, monkeypatch, tmp_path):
    pid, _ = register(client, repo, kind='airflow')
    monkeypatch.setattr(get_settings(), 'data_dir', str(tmp_path))
    endpoint = f'/v1/workloads/{pid}/orchestrator-runs'
    result = client.get(endpoint, headers=as_('ana'))
    assert result.status_code == 503
    assert not list(tmp_path.rglob('airflow.db'))
    def failed(*args):
        raise RuntimeError('postgresql://admin:SECRET@db/private')
    monkeypatch.setattr(airflow_history, 'recent_runs', failed)
    result = client.get(endpoint, headers=as_('ana'))
    assert result.status_code == 503 and 'SECRET' not in result.text and 'private' not in result.text
    other, _ = register(client, repo, kind='app', slug='dashboard')
    assert client.get(f'/v1/workloads/{other}/orchestrator-runs', headers=as_('ana')).status_code == 409


def test_distributed_history_uses_only_its_deployment_login_in_read_only_mode(client, repo, monkeypatch, tmp_path):
    pid, _ = register(client, repo, kind='airflow')
    store = deps.get_secret_store()
    store.put('airflow/' + pid, {'password': 'DEPLOYMENT PASSWORD'})
    monkeypatch.setattr(get_settings(), 'airflow_metadata_url', 'postgresql://platform:ADMIN@metadata:5432/postgres')
    engine, _ = metadata(tmp_path, pid)
    captured = []
    def local_engine(url, **kwargs):
        captured.append((url, kwargs))
        return engine
    monkeypatch.setattr(airflow_history, 'create_engine', local_engine)
    with SessionLocal() as db:
        pub = db.get(Workload, pid)
        pub.program_def = {'airflow_settings': {'mode': 'distributed'}}
        db.commit()
    result = client.get(f'/v1/workloads/{pid}/orchestrator-runs', headers=as_('ana'))
    assert result.status_code == 200, result.text
    url, options = captured[0]
    assert url.username == url.database == 'airflow_' + pid and url.password == 'DEPLOYMENT PASSWORD'
    assert options['connect_args']['connect_timeout'] == 3
    assert 'default_transaction_read_only=on' in options['connect_args']['options']
    assert 'DEPLOYMENT PASSWORD' not in result.text and 'ADMIN' not in result.text
