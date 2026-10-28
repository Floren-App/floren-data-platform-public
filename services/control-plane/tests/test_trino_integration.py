"""Run with FDP_TEST_TRINO_URL, FDP_TEST_PG_URL and FDP_TEST_S3_URL.

The disposable compose project in sql-integration/ provides the backends. Trino calls
the same app's policy endpoints over a short-lived test server on port 18347.
"""
import os
import threading
import time
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from app import rbac, sql_access
from app.auth import User, mint_token
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import DataSource, DirectoryPrincipal, Resource, RoleAssignment, Run, Workspace
from .conftest import as_
from .rbac_helpers import grant_group

pytestmark = pytest.mark.skipif(not os.environ.get('FDP_TEST_TRINO_URL'), reason='live Trino not configured')


@pytest.fixture
def live_sql(client, monkeypatch):
    import psycopg
    import boto3
    import uvicorn
    pg = urlsplit(os.environ['FDP_TEST_PG_URL'])
    with psycopg.connect(os.environ['FDP_TEST_PG_URL'], autocommit=True) as conn:
        conn.execute('DROP SCHEMA IF EXISTS reporting CASCADE')
        conn.execute('DROP SCHEMA IF EXISTS private CASCADE')
        conn.execute('CREATE SCHEMA reporting')
        conn.execute('CREATE SCHEMA private')
        conn.execute('CREATE TABLE reporting.sales(id int primary key, amount int)')
        conn.execute('INSERT INTO reporting.sales VALUES (1,10),(2,20)')
        conn.execute('CREATE TABLE private.hidden(id int)')
        conn.execute('INSERT INTO private.hidden VALUES(99)')
        conn.execute('CREATE VIEW reporting.leak AS SELECT * FROM private.hidden')
    s3 = boto3.client('s3', endpoint_url=os.environ['FDP_TEST_S3_URL'],
                      aws_access_key_id='fdp', aws_secret_access_key='fdp-rustfs-secret')
    try:
        s3.create_bucket(Bucket='lake')
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    settings = get_settings()
    monkeypatch.setattr(settings, 'query_executor', 'trino')
    monkeypatch.setattr(settings, 'trino_url', os.environ['FDP_TEST_TRINO_URL'])
    monkeypatch.setattr(settings, 'trino_signing_key', 'dev-only-trino-key-change-me-01234567890123456789')
    monkeypatch.setattr(settings, 'internal_url', 'http://127.0.0.1:18347')
    sql_access._client = None
    if os.environ.get('FDP_DEBUG_SQL'):
        original = sql_access.policy_allow
        def debug(query, action):
            print('POLICY INPUT', action, flush=True)
            result = original(query, action)
            print('POLICY', action, list(query.scope), result, flush=True)
            return result
        monkeypatch.setattr(sql_access, 'policy_allow', debug)
        original_query = sql_access.policy_query
        def debug_query(db, payload):
            try:
                return original_query(db, payload)
            except Exception as exc:
                print('POLICY CONTEXT ERROR', str(exc), flush=True)
                raise
        monkeypatch.setattr(sql_access, 'policy_query', debug_query)
    server = uvicorn.Server(uvicorn.Config(app, host='0.0.0.0', port=18347, lifespan='off', log_level='warning', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)
    assert server.started
    try:
        for name, kind in [('finance', 'postgres'), ('lake', 'iceberg')]:
            body = {'name': name, 'kind': kind, 'config': {'catalog': name}}
            if kind == 'iceberg':
                body['config'].update(warehouse='s3://lake/', catalog_uri='http://iceberg-rest:8181')
            if kind == 'postgres':
                body['config'].update(host=pg.hostname, port=pg.port, database='finance')
                body['secret'] = {'username': 'fdp_trino', 'password': 'dev-only-trino-warehouse'}
                body['provisioner_secret'] = {'username': pg.username, 'password': pg.password}
            client.put('/v1/admin/datasources/' + name, headers=as_('admin'), json=body).raise_for_status()
            for schema_name in ['reporting', 'private']:
                schema = client.post('/v1/rbac/sources/' + name + '/schemas', headers=as_('admin'), json={'name': schema_name}).json()
                response = client.post('/v1/rbac/resources/' + schema['id'] + '/provision', headers=as_('admin'))
                assert response.status_code == 200, response.text
                if schema_name == 'reporting':
                    grant_group(client, schema['id'], name + '-reader', 'Reader', users=('ana',))
        yield client
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sql_access._client = None


def request(client, statement, user='ana', source='finance', **kwargs):
    return client.post('/v1/sql/query', headers=as_(user), json={'source': source, 'sql': statement, **kwargs})


def test_live_shared_engine_roles_and_cross_catalog(live_sql):
    client = live_sql
    response = request(client, 'SELECT * FROM finance.reporting.sales ORDER BY id')
    assert response.status_code == 200, response.text
    assert response.json()['rows'] == [[1, 10], [2, 20]]
    assert request(client, 'SELECT * FROM finance.private.hidden').status_code == 422
    assert request(client, 'SELECT * FROM finance.reporting.leak').status_code == 422
    assert request(client, 'INSERT INTO finance.reporting.sales VALUES(3,30)').status_code == 422
    assert request(client, 'CREATE TABLE finance.reporting.denied(id int)').status_code == 422
    # SQL clients (JDBC, CloudBeaver) browse through system.jdbc; rows follow the same grants.
    response = request(client, "SELECT table_cat, table_schem, table_name FROM system.jdbc.tables WHERE table_cat = 'finance'")
    assert response.status_code == 200, response.text
    assert ['finance', 'reporting', 'sales'] in response.json()['rows']
    assert not any(row[1] == 'private' for row in response.json()['rows'])
    response = request(client, "SELECT table_schem FROM system.jdbc.columns WHERE table_cat = 'finance' AND table_name = 'hidden'")
    assert response.status_code == 200 and response.json()['rows'] == []
    response = request(client, 'SHOW SCHEMAS FROM finance')
    assert response.status_code == 200, response.text
    assert 'private' not in str(response.json()['rows'])
    response = request(client, "SELECT table_schema,table_name FROM finance.information_schema.tables")
    # The browser uses the same governed information_schema path and never reads sample rows.
    with SessionLocal() as db:
        source_id = db.scalar(select(DataSource.id).where(DataSource.name == 'finance'))
        schema_id = db.scalar(select(Resource.id).where(Resource.object_id == source_id, Resource.name == 'reporting'))
    objects = client.get(f'/v1/data/schemas/{schema_id}/children', headers=as_('ana'))
    assert objects.status_code == 200, objects.text
    assert {'name': 'sales', 'kind': 'table'} in objects.json()['children']
    assert not any(c['name'] in {'leak', 'hidden'} for c in objects.json()['children'])
    columns = client.get(f'/v1/data/schemas/{schema_id}/children', params={'table': 'sales'}, headers=as_('ana'))
    assert columns.status_code == 200, columns.text
    assert [c['name'] for c in columns.json()['children']] == ['id', 'amount']
    preview = client.get(f'/v1/data/schemas/{schema_id}/preview', params={'table': 'sales'}, headers=as_('ana'))
    assert preview.status_code == 200, preview.text
    assert preview.json()['columns'] == ['id', 'amount'] and len(preview.json()['rows']) == 2
    blocked_preview = client.get(f'/v1/data/schemas/{schema_id}/preview', params={'table': 'leak'}, headers=as_('ana'))
    assert blocked_preview.status_code == 422  # A preview cannot bypass view dependency policy.
    assert response.status_code == 200, response.text
    assert 'private' not in str(response.json()['rows'])
    # Owner uses normal API assignment; it never needs Trino GRANT statements.
    for catalog in ['finance', 'lake']:
        with SessionLocal() as db:
            ds = db.scalar(select(DataSource).where(DataSource.name == catalog))
            resource = db.scalar(select(Resource).where(Resource.object_id == ds.id, Resource.name == 'reporting'))
            rid = resource.id
        grant_group(client, rid, catalog + '-writer', 'Contributor', users=('ana',))
    assert request(client, 'INSERT INTO finance.reporting.sales VALUES(3,30)').status_code == 200
    assert request(client, 'CREATE TABLE finance.reporting.denied(id int)').status_code == 422
    response = request(client, 'CREATE SCHEMA IF NOT EXISTS lake.reporting', user='admin', source='lake')
    assert response.status_code == 200, response.text
    response = request(client, 'DROP TABLE IF EXISTS lake.reporting.sales', user='admin', source='lake')
    assert response.status_code == 200, response.text
    response = request(client, 'CREATE TABLE lake.reporting.sales AS SELECT * FROM finance.reporting.sales', user='admin', source='lake')
    assert response.status_code == 200, response.text
    response = request(client, 'SELECT count(*) FROM finance.reporting.sales p JOIN lake.reporting.sales l ON p.id=l.id')
    assert response.status_code == 200, response.text
    assert response.json()['rows'] == [[3]]
    assert request(client, 'INSERT INTO lake.reporting.sales VALUES(4,40)', source='lake').status_code == 200
    assert request(client, 'CREATE TABLE lake.reporting.denied(id int)', source='lake').status_code == 422
    # Arbitrary native SQL and external file registration bypass table authorization.
    assert request(client, "CALL finance.system.execute(query => 'SELECT 1')", user='admin').status_code == 422
    assert request(client, "SELECT * FROM TABLE(finance.system.query(query => 'SELECT * FROM private.hidden'))", user='admin').status_code == 422
    assert request(client, "CREATE TABLE lake.reporting.escape(id int) WITH (location='s3://lake/private/escape')", user='admin', source='lake').status_code == 422
    # A prepared parameter stays a value, including quotes and comment characters.
    response = request(client, 'SELECT ? as value', parameters=["x'; SELECT * FROM private.hidden --"])
    assert response.status_code == 200, response.text
    assert response.json()['rows'] == [["x'; SELECT * FROM private.hidden --"]]


def test_live_ddl_future_tables_views_and_results(live_sql):
    client = live_sql
    for catalog in ['finance', 'lake']:
        with SessionLocal() as db:
            ds = db.scalar(select(DataSource).where(DataSource.name == catalog))
            rid = db.scalar(select(Resource.id).where(Resource.object_id == ds.id, Resource.name == 'reporting'))
        grant_group(client, rid, catalog + '-admin', 'Admin', users=('bob',))
    for source in ['finance', 'lake']:
        assert request(client, f'CREATE SCHEMA IF NOT EXISTS {source}.reporting', user='bob', source=source).status_code == 200
        request(client, f'DROP TABLE IF EXISTS {source}.reporting.future', user='bob', source=source)
        response = request(client, f'CREATE TABLE {source}.reporting.future(id int)', user='bob', source=source)
        assert response.status_code == 200, response.text
        assert request(client, f'INSERT INTO {source}.reporting.future VALUES(7)', user='bob', source=source).status_code == 200
        assert request(client, f'SELECT * FROM {source}.reporting.future', source=source).json()['rows'] == [[7]]
        assert request(client, f'ALTER TABLE {source}.reporting.future ADD COLUMN note varchar', user='bob', source=source).status_code == 200
        assert request(client, f'GRANT SELECT ON {source}.reporting.future TO ana', user='bob', source=source).status_code == 422
    request(client, 'DROP VIEW IF EXISTS lake.reporting.invoker_view', user='admin', source='lake')
    response = request(client, 'CREATE VIEW lake.reporting.invoker_view SECURITY INVOKER AS SELECT * FROM finance.reporting.sales', user='bob', source='lake')
    # Bob needs source Reader separately, just like any cross-catalog query.
    assert response.status_code == 200, response.text
    assert request(client, 'SELECT * FROM lake.reporting.invoker_view', source='lake').status_code == 200
    assert request(client, 'CREATE OR REPLACE VIEW lake.reporting.bad SECURITY DEFINER AS SELECT * FROM finance.reporting.sales', user='bob', source='lake').status_code == 422
    response = request(client, 'SELECT * FROM UNNEST(sequence(1,100))', max_rows=5)
    assert response.status_code == 200, response.text
    assert len(response.json()['rows']) == 5 and response.json()['truncated']


def test_live_revocation_retains_jobs_and_binds_viewers(live_sql):
    client = live_sql
    from app.models import ComputePool, GitRepository, Workload
    grant_group(client, 'workloads', 'deployers', 'Owner', users=('admin', 'ana'))
    with SessionLocal() as db:
        repo = GitRepository(name='sql-repo', url='https://example.test/repo.git')
        db.add(repo)
        db.flush()
        pub = Workload(slug='sql-viewer', title='SQL viewer', repo_id=repo.id, branch='main', program='app',
                          kind='app', runtime='python', owner='ana', warehouse='small', status='live', deployed_sha='a'*40)
        db.add(pub)
        db.flush()
        rbac.workload_resource(db, pub)
        db.commit()
        app_token = mint_token('execution', pub.identity, 3600, pub=pub.id, pid=pub.service_principal_id, sha=pub.deployed_sha)
    with SessionLocal() as db:
        user = rbac.resolve_user(db, User('ana'))
        pid = user.principal_id
        ws = Workspace(name='retained job', owner='ana', principal_id=pid, editor='vscode', status='running')
        db.add(ws)
        db.flush()
        from app.data_access import snapshot
        job = Run(owner='ana', principal_id=pid, workspace_id=ws.id, pool_id=db.scalar(select(ComputePool.id)),
                  kind='script', entrypoint='report.py', status='running', access_snapshot=snapshot(db, user))
        db.add(job)
        db.commit()
        job_token = mint_token('execution', 'ana', 3600, run=job.id, pid=pid)
        expired_job_token = mint_token('execution', 'ana', -60, run=job.id, pid=pid)
    viewer = mint_token('viewer', 'ana', 120, pid=pid, pub=pub.id)
    headers = {'Authorization': 'Bearer ' + app_token, 'X-FDP-Viewer': viewer}
    body = {'source': 'finance', 'sql': 'SELECT * FROM finance.reporting.sales'}
    response = client.post('/v1/delegated/query', headers=headers, json=body)
    assert response.status_code == 200, response.text
    response = client.post('/v1/sql/query', headers={'Authorization': 'Bearer ' + expired_job_token}, json=body)
    assert response.status_code == 200, response.text
    assert client.post('/v1/sql/query', headers={'Authorization': headers['Authorization']}, json=body).status_code == 403
    with SessionLocal() as db:
        db.query(RoleAssignment).filter(RoleAssignment.role == 'Reader').delete()
        db.commit()
    assert client.post('/v1/delegated/query', headers=headers, json=body).status_code in (403,404)
    response = client.post('/v1/sql/query', headers={'Authorization': 'Bearer ' + job_token}, json=body)
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        db.get(DirectoryPrincipal, pid).active = False
        db.commit()
    response = client.post('/v1/sql/query', headers={'Authorization': 'Bearer ' + job_token}, json=body)
    assert response.status_code == 200, response.text
    assert client.post('/v1/delegated/query', headers=headers, json=body).status_code in (401,403)
    with SessionLocal() as db:
        db.get(Run, job.id).status = 'succeeded'
        db.commit()
    assert client.post('/v1/sql/query', headers={'Authorization': 'Bearer ' + job_token}, json=body).status_code == 401
    assert client.post('/v1/sql/query', headers={'Authorization': 'Bearer ' + expired_job_token}, json=body).status_code == 401
    # A fixed workload uses its explicitly assigned service grants, independently
    # of the disabled human viewer/deployer.
    with SessionLocal() as db:
        fixed = db.get(Workload, pub.id)
        fixed.execution_mode = 'fixed'
        source = db.scalar(select(DataSource).where(DataSource.name == 'finance'))
        resource_id = db.scalar(select(Resource.id).where(Resource.object_id == source.id, Resource.name == 'reporting'))
        db.commit()
    grant_group(client, resource_id, 'App service readers', 'Reader', services=(pub.service_principal_id,))
    response = client.post('/v1/sql/query', headers={'Authorization': 'Bearer ' + app_token}, json=body)
    assert response.status_code == 200, response.text


def test_live_python_r_and_dbt_clients(live_sql, monkeypatch, tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    client = live_sql
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / 'sdk/python'))
    with SessionLocal() as db:
        user = rbac.resolve_user(db, User('ana'))
        ws = Workspace(name='SDK test', owner='ana', principal_id=user.principal_id, editor='vscode', status='running')
        db.add(ws)
        db.commit()
        token = mint_token('execution', 'ana', 3600, ws=ws.id, pid=user.principal_id)
    monkeypatch.setenv('FDP_API', 'http://127.0.0.1:18347')
    monkeypatch.setenv('FDP_SESSION_TOKEN', token)
    import fdp
    engine = fdp.connect('finance')
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT count(*) FROM reporting.sales').scalar_one() == 2
        assert conn.exec_driver_sql('SELECT ? as value', (17,)).scalar_one() == 17
    engine.dispose()
    # Optional tools are enabled explicitly so the normal backend suite stays lightweight.
    if os.environ.get('FDP_TEST_R_LIBS'):
        env = {**os.environ, 'R_LIBS': os.environ['FDP_TEST_R_LIBS']}
        code = f'''library(DBI)
for(f in list.files("{root}/sdk/r/fdp/R", full.names=TRUE)) source(f)
con <- connect("finance")
df <- dbGetQuery(con, "SELECT count(*) AS n FROM reporting.sales")
stopifnot(as.integer(df$n) == 2L)
stopifnot(dbGetQuery(con, "SELECT ? AS value", params=list("x'; --"))$value == "x'; --")
res <- dbSendQuery(con, "SELECT * FROM reporting.sales ORDER BY id")
stopifnot(nrow(dbFetch(res, n=1)) == 1L, nrow(dbFetch(res)) == 1L)
dbClearResult(res)
dbDisconnect(con)
'''
        result = subprocess.run(['Rscript', '-e', code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
    dbt = os.environ.get('FDP_TEST_DBT')
    if dbt:
        for source in ['finance', 'lake']:
            with SessionLocal() as db:
                ds = db.scalar(select(DataSource).where(DataSource.name == source))
                rid = db.scalar(select(Resource.id).where(Resource.object_id == ds.id, Resource.name == 'reporting'))
            grant_group(client, rid, source + '-builder', 'Admin', users=('ana',))
            project = tmp_path / source
            (project / 'models').mkdir(parents=True)
            (project / 'dbt_project.yml').write_text('name: smoke\nversion: "1.0"\nprofile: smoke\nmodel-paths: [models]\nmodels:\n  smoke:\n    +materialized: table\n')
            (project / 'models' / 'shared_smoke.sql').write_text('select 42 as id')
            from fdp.dbt import profile
            (project / 'profiles.yml').write_text(__import__('json').dumps(profile(source, 'reporting', threads=1, project_dir=str(project))))
            result = subprocess.run([dbt, 'run', '--project-dir', str(project), '--profiles-dir', str(project)],
                                    capture_output=True, text=True, timeout=120)
            assert result.returncode == 0, result.stdout + result.stderr


def test_live_small_query_benchmark(live_sql, monkeypatch):
    """Opt-in local measurement, never a flaky latency threshold assertion."""
    output = os.environ.get('FDP_TEST_SQL_BENCHMARK')
    if not output:
        pytest.skip('benchmark report path not requested')
    import psycopg
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    with psycopg.connect(os.environ['FDP_TEST_PG_URL'], autocommit=True) as conn:
        conn.execute('CREATE TABLE reporting.bench(id int primary key,amount int)')
        conn.execute('INSERT INTO reporting.bench SELECT g,g%100 FROM generate_series(1,50000) g')
        conn.execute('GRANT SELECT ON reporting.bench TO fdp_trino')
        conn.execute('ANALYZE reporting.bench')
    with SessionLocal() as db:
        user = rbac.resolve_user(db, User('ana'))
        ws = Workspace(name='benchmark', owner='ana', principal_id=user.principal_id, editor='vscode', status='running')
        db.add(ws)
        db.commit()
        token = mint_token('execution', 'ana', 3600, ws=ws.id, pid=user.principal_id)
    env = {**os.environ, 'PYTHONPATH': str(root / 'sdk/python'), 'FDP_API': 'http://127.0.0.1:18347',
           'FDP_SESSION_TOKEN': token, 'FDP_BENCH_PG_URL': os.environ['FDP_TEST_PG_URL']}
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('benchmark_sql.py')),
        '--output', output, '--iterations', '20', '--concurrency', '4'], env=env,
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
