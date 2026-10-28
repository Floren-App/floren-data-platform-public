import httpx
from sqlalchemy import select

from app import deps, rbac, sql_access
from app.auth import User
from app.config import get_settings
from app.db import SessionLocal
from app.models import Resource, DataSource
from app.orchestrator.base import ExecutionSpec
from .conftest import as_
from .test_shared_sql import sql_mode  # noqa: F401
from .test_deploying import repo, deploy  # noqa: F401


def test_directory_distinguishes_idp_memberships_and_app_security_groups(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'scim_token', 'directory-test')
    headers = {'Authorization': 'Bearer directory-test'}
    client.get('/v1/me', headers=as_('admin')).raise_for_status()
    ana = client.post('/scim/v2/Users', headers=headers, json={'userName': 'ana', 'roles': [{'value': 'fdp-admin'}]}).json()
    external = client.post('/scim/v2/Groups', headers=headers, json={'displayName': 'IdP finance', 'members': [{'value': ana['id']}]}).json()
    with SessionLocal() as db:
        admin = rbac.resolve_user(db, User('admin'))
    group = client.post('/v1/rbac/groups', headers=as_('admin'), json={'name': 'Finance security', 'owners': [admin.principal_id]}).json()
    client.post(f"/v1/rbac/groups/{group['id']}/bindings", headers=as_('admin'), json={'principal_id': external['id']}).raise_for_status()
    rows = client.get('/v1/rbac/directory', headers=as_('admin')).json()
    output = next(p for p in rows if p['id'] == ana['id'])
    assert output['origin'] == 'idp' and output['provisioning'] == 'scim' and output['platform_admin']
    assert external['id'] in output['member_of'] and group['id'] in output['app_groups']
    assert group['origin'] == 'app'
    assert client.get('/v1/rbac/directory', headers=as_('bob')).status_code == 403
    # Group ownership does not allow editing the external directory or resource grants.
    assert client.put('/v1/rbac/directory/' + ana['id'], headers=as_('admin'), json={'platform_admin': True}).status_code in (404, 405)


def test_compute_inventory_includes_other_users_and_airflow_components(client):
    orch = deps.get_orchestrator()
    for name, role, workload, state in [('other-job', 'compute', None, 'running'), ('web', 'workload', 'airflow', 'running'),
                                         ('scheduler', 'workload', 'airflow', 'running'), ('worker', 'workload', 'airflow', 'pending'),
                                         ('finished', 'compute', None, 'succeeded')]:
        labels = {'fdp.floren.app/pool': 'small', 'fdp.floren.app/role': role}
        if workload:
            labels['fdp.floren.app/workload'] = workload
        orch.start(ExecutionSpec(name=name, image='test', cpu=1, memory_gb=1, labels=labels))
        orch.states[name] = state
    pools = client.get('/v1/me', headers=as_('bob')).json()['pools']
    small = next(p for p in pools if p['name'] == 'small')
    assert small['active_executions'] == 4 and small['active_runs'] == 1 and small['active_services'] == 3
    assert small['active_products'] == 1 and small['usage_available']
    assert client.get('/v1/runs', headers=as_('bob')).json() == []  # Counts do not expose other users' runs.


def test_inventory_failure_does_not_report_zero(client, monkeypatch):
    def unavailable():
        raise RuntimeError('offline')
    monkeypatch.setattr(deps.get_orchestrator(), 'inventory', unavailable)
    pools = client.get('/v1/me', headers=as_('ana')).json()['pools']
    assert all(p['active_executions'] is None and not p['usage_available'] for p in pools)


def test_group_ownership_does_not_expose_or_assign_private_resources(client):
    client.get('/v1/me', headers=as_('ana')).raise_for_status()
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
    group = client.post('/v1/rbac/groups', headers=as_('admin'), json={'name': 'Delegated group', 'owners': [ana.principal_id]}).json()
    folder = client.post('/v1/rbac/folders', headers=as_('admin'), json={'name': 'Private folder'}).json()
    response = client.put(f"/v1/rbac/resources/{folder['id']}/assignments", headers=as_('admin'), json={'app_group_id': group['id'], 'role': 'Executor'})
    assert response.status_code == 200
    url = f"/v1/rbac/groups/{group['id']}/assignments"
    assert client.get(url, headers=as_('admin')).json()[0]['resource']['id'] == folder['id']
    assert client.get(url, headers=as_('ana')).json() == []
    assert client.get(url, headers=as_('bob')).status_code == 403
    assert client.put(f"/v1/rbac/resources/{folder['id']}/assignments", headers=as_('ana'), json={'app_group_id': group['id'], 'role': 'Owner'}).status_code == 403


def test_script_is_callable_without_a_persistent_workload(client, repo):
    workload = deploy(client, repo, program='sales-report', slug='callable-report').json()
    assert workload['status'] == 'live' and workload['execution_state'] == 'on_demand'
    assert not deps.get_orchestrator().executions
    response = client.post(f"/v1/workloads/{workload['id']}/runs", headers=as_('ana'), json={'args': {'start_date': '2026-01-01'}})
    assert response.status_code == 201, response.text
    small = next(p for p in client.get('/v1/me', headers=as_('ana')).json()['pools'] if p['name'] == response.json()['pool_name'])
    assert small['active_runs'] == 1 and small['active_services'] == 0


def test_resource_navigation_excludes_retired_and_missing_workloads_but_keeps_iam_history(client, repo):
    active = deploy(client, repo, program='sales-report', slug='active-report').json()
    retired = deploy(client, repo, program='explore', slug='retired-report').json()
    before = client.get('/v1/rbac/resources', headers=as_('admin')).json()
    assert {active['resource_id'], retired['resource_id']} <= {r['id'] for r in before}
    assert client.delete(f"/v1/workloads/{retired['id']}", headers=as_('ana')).status_code == 204
    with SessionLocal() as db:
        assert db.get(Resource, retired['resource_id']) is not None
        orphan = Resource(kind='service', name='Missing workload', object_id='missing-pub', parent_id='workloads')
        db.add(orphan); db.flush()
        orphan_id = orphan.id
        db.commit()
    after = client.get('/v1/rbac/resources', headers=as_('admin')).json()
    ids = {r['id'] for r in after}
    assert active['resource_id'] in ids
    assert retired['resource_id'] not in ids and orphan_id not in ids
    with SessionLocal() as db:
        assert db.get(Resource, retired['resource_id']) is not None


def test_preview_is_bounded_quoted_authorized_and_not_part_of_metadata(sql_mode, monkeypatch):
    client, _ = sql_mode
    sent = []
    async def engine(method, path, **kwargs):
        sent.append(kwargs['content'].decode())
        return httpx.Response(200, json={'id': 'preview', 'columns': [{'name': 'id'}, {'name': 'amount'}],
            'data': [[9223372036854775807, None] for _ in range(26)]})
    monkeypatch.setattr(sql_access, 'engine_request', engine)
    with SessionLocal() as db:
        ds = db.scalar(select(DataSource).where(DataSource.name == 'finance'))
        rid = db.scalar(select(Resource.id).where(Resource.object_id == ds.id, Resource.kind == 'schema'))
    url = f'/v1/data/schemas/{rid}/preview'
    assert client.get(url, headers=as_('bob'), params={'table': 'sales'}).status_code == 404
    assert sent == []
    response = client.get(url, headers=as_('ana'), params={'table': 'sales"; DROP TABLE private.hidden --'})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert len(response.json()['rows']) == 25 and response.json()['rows'][0][0] == '9223372036854775807'
    assert sent[0] == 'SELECT * FROM "finance"."reporting"."sales""; DROP TABLE private.hidden --" LIMIT 25'
