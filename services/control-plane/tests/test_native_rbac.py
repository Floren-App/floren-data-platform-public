"""Real SQL verification. Set FDP_TEST_PG_URL to an isolated disposable database."""
import os
import uuid
from datetime import timedelta
import pytest
from sqlalchemy import select
from app import data_access, deps
from app.auth import User
from app.db import SessionLocal
from app.models import DataSource, Resource, CredentialLease, Run, utcnow
from .conftest import as_
from .rbac_helpers import grant_group
from .test_api import start_workspace, execution_token
from .test_deploying import repo, deploy  # noqa: F401

@pytest.mark.skipif(not os.getenv('FDP_TEST_PG_URL'), reason='requires disposable PostgreSQL')
def test_real_postgres_roles_future_objects_and_revoked_running_job(client, repo):
    import psycopg
    from psycopg import sql
    from urllib.parse import urlsplit
    cfg = urlsplit(os.environ['FDP_TEST_PG_URL'])
    schema = 'rbac_' + uuid.uuid4().hex[:10]
    admin = psycopg.connect(os.environ['FDP_TEST_PG_URL'], autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    admin.execute(sql.SQL('CREATE TABLE {}.sales (id serial PRIMARY KEY, value text)').format(sql.Identifier(schema)))
    admin.execute(sql.SQL("INSERT INTO {}.sales(value) VALUES ('hello')").format(sql.Identifier(schema)))
    manager = {'username': cfg.username, 'password': cfg.password}
    dsbody = {'name': 'native-test', 'kind': 'postgres', 'config': {'host': cfg.hostname, 'port': cfg.port, 'database': cfg.path[1:], 'catalog': 'native_test'},
              'provisioner_secret': manager}
    source_response = client.put('/v1/admin/datasources/native-test', headers=as_('admin'), json=dsbody)
    assert source_response.status_code == 200, source_response.text
    source_id = source_response.json()['id']
    r = client.post('/v1/rbac/sources/native-test/schemas', headers=as_('admin'), json={'name': schema}).json()
    roles = data_access.role_names(source_id, schema)
    try:
        ready = client.post(f"/v1/rbac/resources/{r['id']}/provision", headers=as_('admin'))
        assert ready.status_code == 200, ready.text
        gid = grant_group(client, r['id'], 'Native readers', 'Reader', users=['ana'])
        ws = start_workspace(client)
        _, wh = execution_token(ws)
        info = client.get('/v1/connect/native-test', headers=wh)
        assert info.status_code == 200, info.text
        def connection(cred):
            return psycopg.connect(host=cfg.hostname, port=cfg.port, dbname=cfg.path[1:],
                                   user=cred['username'], password=cred['password'], autocommit=True)
        reader = connection(info.json()['credentials'])
        assert reader.execute('SELECT value FROM sales').fetchone() == ('hello',)
        for denied in ('INSERT INTO sales(value) VALUES (\'no\')', 'ALTER TABLE sales ADD COLUMN no int',
                       'CREATE TABLE forbidden(id int)', 'TRUNCATE sales'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader.execute(denied)
        grant_group(client, r['id'], 'Native writers', 'Contributor', users=['bob'])
        bw = start_workspace(client, 'bob')
        _, bh = execution_token(bw)
        wi = client.get('/v1/connect/native-test', headers=bh).json()['credentials']
        writer = connection(wi)
        writer.execute("INSERT INTO sales(value) VALUES ('written')")
        writer.execute("UPDATE sales SET value='updated' WHERE value='written'")
        writer.execute("DELETE FROM sales WHERE value='updated'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute('CREATE TABLE forbidden(id int)')
        grant_group(client, r['id'], 'Native ddl', 'Admin', users=['admin'])
        aws = start_workspace(client, 'admin')
        _, ah = execution_token(aws)
        ai = client.get('/v1/connect/native-test', headers=ah).json()['credentials']
        ddl = connection(ai)
        # No SDK-side SQL filtering: default login role makes future objects use stable ownership.
        ddl.execute('CREATE TABLE future_object(id int)')
        assert reader.execute('SELECT * FROM future_object').fetchall() == []
        ddl.execute('ALTER TABLE future_object ADD COLUMN label text')
        ddl.execute(sql.SQL('GRANT SELECT ON future_object TO {}').format(sql.Identifier(wi['username'])))
        # A native Admin can grant outside the application model; new lower-role
        # connections must detect that drift instead of issuing a falsely read-only login.
        ddl.execute(sql.SQL('GRANT INSERT ON sales TO {}').format(sql.Identifier(roles['Reader'])))
        assert client.get('/v1/connect/native-test', headers=wh).status_code == 409
        ddl.execute(sql.SQL('REVOKE INSERT ON sales FROM {}').format(sql.Identifier(roles['Reader'])))
        # Real viewer-bound shared-app queries never expose viewer database credentials.
        from app.auth import mint_token
        from app import rbac
        workload = deploy(client, repo, program='sales-dashboard', slug='viewer-app').json()
        app_spec = deps.get_orchestrator().executions[f"pub-{workload['id']}-{workload['deployed_sha'][:7]}"]
        with SessionLocal() as db:
            viewer = rbac.resolve_user(db, User('ana'))
            viewer_id = viewer.principal_id
        viewer_token = mint_token('viewer', 'ana', 120, pid=viewer_id, pub=workload['id'])
        app_bearer = {'Authorization': 'Bearer ' + app_spec.env['FDP_SESSION_TOKEN'], 'X-FDP-Viewer': viewer_token}
        query_body = {'source': 'native-test', 'sql': f'SELECT value FROM "{schema}".sales'}
        query = client.post('/v1/delegated/query', headers=app_bearer, json=query_body)
        assert query.status_code == 200, query.text
        assert query.json()['columns'] == ['value'] and query.json()['rows'] == [['hello']]
        assert 'password' not in query.text
        denied_write = client.post('/v1/delegated/query', headers=app_bearer,
            json={'source': 'native-test', 'sql': f"INSERT INTO \"{schema}\".sales(value) VALUES ('denied')"})
        assert denied_write.status_code == 422
        wrong_pub = {**app_bearer, 'X-FDP-Viewer': mint_token('viewer', 'ana', 120, pid=viewer_id, pub='other')}
        assert client.post('/v1/delegated/query', headers=wrong_pub, json=query_body).status_code == 403
        # Snapshot the admitted job, then remove interactive/new access.
        run = client.post('/v1/runs', headers=as_('ana'), json={'workspace_id': ws, 'pool': 'small', 'kind': 'script', 'entrypoint': 'job.py'}).json()
        token = deps.get_orchestrator().executions[f"run-{run['id']}"] .env['FDP_SESSION_TOKEN']
        rh = {'Authorization': 'Bearer ' + token}
        client.delete(f"/v1/rbac/resources/{r['id']}/assignments/{gid}", headers=as_('admin'))
        assert client.get('/v1/connect/native-test', headers=wh).status_code == 404
        assert client.post('/v1/delegated/query', headers=app_bearer, json=query_body).status_code == 404
        retained = client.get('/v1/connect/native-test', headers=rh)
        assert retained.status_code == 200, retained.text
        running = connection(retained.json()['credentials'])
        assert running.execute('SELECT value FROM sales').fetchone() == ('hello',)
        client.post(f"/v1/runs/{run['id']}/stop", headers=as_('ana'))
        assert client.get('/v1/connect/native-test', headers=rh).status_code == 401
        with SessionLocal() as db:
            data_access.cleanup_leases(db, deps.get_secret_store())
        with pytest.raises(psycopg.OperationalError):
            running.execute('SELECT 1')
        # Retiring an Admin login must preserve its objects, including objects it created
        # under its own transient login rather than SET ROLE to the stable owner.
        with SessionLocal() as db:
            lease = db.scalar(select(CredentialLease).where(CredentialLease.username == ai['username']))
            lease.expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
            data_access.cleanup_leases(db, deps.get_secret_store())
        assert reader.execute('SELECT * FROM future_object').fetchall() == []
        actual_owner = admin.execute("SELECT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname='future_object'", (schema,)).fetchone()[0]
        assert actual_owner == roles['Admin']
        for c in (reader, writer, ddl, running):
            c.close()
    finally:
        # This fixture is only permitted against a disposable database.
        admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename LIKE \'dcl_%\' AND pid<>pg_backend_pid()')
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        leases = admin.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'dcl_%'").fetchall()
        existing_roles = {r[0] for r in admin.execute('SELECT rolname FROM pg_roles').fetchall()}
        for (name,) in leases + [(name,) for name in set(roles.values()) if name in existing_roles]:
            admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(name)))
            admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(name)))
        admin.close()


@pytest.mark.skipif(not os.getenv('FDP_TEST_PG_URL'), reason='requires disposable PostgreSQL')
def test_concurrent_directory_claim_refresh_is_atomic():
    import concurrent.futures
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    from app import rbac
    from app.models import DirectoryPrincipal, DirectoryMembership
    schema = 'cp_' + uuid.uuid4().hex[:10]
    engine = create_engine(os.environ['FDP_TEST_PG_URL'].replace('postgresql://', 'postgresql+psycopg://', 1),
                          connect_args={'options': f'-c search_path={schema}'})
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA {schema}'))
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        def refresh(_):
            with sessions() as db:
                u = rbac.resolve_user(db, User('concurrent', subject='same-oidc-subject', groups=['finance'], roles=['analyst']), refresh=True)
                db.commit()
                return u.principal_id
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(refresh, range(18)))
        assert len(set(ids)) == 1
        with sessions() as db:
            assert len(list(db.scalars(select(DirectoryPrincipal)))) == 3
            assert len(list(db.scalars(select(DirectoryMembership)))) == 2
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        engine.dispose()
