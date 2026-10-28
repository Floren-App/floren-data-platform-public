"""Authorization regressions across directory, folders, deployment, credentials, and sessions."""
from urllib.parse import urlsplit
from sqlalchemy import select, delete
from app import rbac, deps
from app.auth import User, mint_token, read_token
from app.db import SessionLocal
from app.models import (AppGroup, DirectoryPrincipal, DirectoryMembership, IdentityBinding,
                        RoleAssignment, Resource, DataSource, Run)
from .conftest import as_
from .rbac_helpers import grant_group, integration
from .test_api import setup_finance_db, start_workspace, execution_token
from .test_deploying import repo, deploy  # noqa: F401


def test_group_owner_manages_bindings_but_cannot_assign(client):
    for who in ('admin', 'ana', 'bob'):
        client.get('/v1/me', headers=as_(who))
    directory = client.get('/v1/rbac/directory', headers=as_('admin')).json()
    ana = next(p for p in directory if p['name'] == 'ana' and p['kind'] == 'user')
    bob = next(p for p in directory if p['name'] == 'bob' and p['kind'] == 'user')
    group = client.post('/v1/rbac/groups', headers=as_('admin'), json={'name': 'Delegated', 'owners': [ana['id']]}).json()
    assert client.post('/v1/rbac/groups', headers=as_('ana'), json={'name': 'No', 'owners': [ana['id']]}).status_code == 403
    assert client.post(f"/v1/rbac/groups/{group['id']}/bindings", headers=as_('bob'), json={'principal_id': bob['id']}).status_code == 403
    assert client.post(f"/v1/rbac/groups/{group['id']}/bindings", headers=as_('ana'), json={'principal_id': bob['id']}).status_code == 201
    assert client.put('/v1/rbac/resources/workloads/assignments', headers=as_('ana'),
                      json={'app_group_id': group['id'], 'role': 'Owner'}).status_code == 403
    assert client.put(f"/v1/rbac/groups/{group['id']}", headers=as_('ana'),
                      json={'name': 'Delegated', 'owners': [bob['id']]}).status_code == 403
    assert client.put(f"/v1/rbac/groups/{group['id']}", headers=as_('admin'),
                      json={'name': 'Delegated', 'owners': []}).status_code == 422


def test_folder_inheritance_is_additive_and_cycles_are_rejected(client):
    setup_finance_db(client)
    parent = client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': 'Finance'}).json()
    child = client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': 'Reports', 'parent_id': parent['id']}).json()
    gid = grant_group(client, parent['id'], 'Report editors', 'Contributor', users=['bob'])
    client.put(f"/v1/rbac/resources/{child['id']}/assignments", headers=as_('ana'),
               json={'app_group_id': gid, 'role': 'Executor'})
    access = client.get(f"/v1/rbac/resources/{child['id']}/access", headers=as_('bob')).json()
    assert 'deploy' in access['resource']['actions']  # weaker child grant does not block parent
    assert client.post('/v1/rbac/folders', headers=as_('bob'), json={'name': 'No', 'parent_id': child['id']}).status_code == 403
    assert client.patch(f"/v1/rbac/resources/{parent['id']}", headers=as_('ana'),
                        json={'name': 'Finance', 'parent_id': child['id']}).status_code == 422
    assert client.delete(f"/v1/rbac/folders/{parent['id']}", headers=as_('ana')).status_code == 409
    assert client.post('/v1/rbac/roles', headers=as_('admin'), json={'name': 'Custom'}).status_code == 405
    assert client.put(f"/v1/rbac/resources/{parent['id']}/assignments", headers=as_('ana'),
                      json={'app_group_id': gid, 'role': 'Custom'}).status_code == 422


def test_contributor_deploys_but_cannot_change_identity_grants_or_delete(client, repo):
    pub = deploy(client, repo, program='sales-report', slug='report').json()
    grant_group(client, pub['resource_id'], 'Editors', 'Contributor', users=['bob'])
    assert client.patch(f"/v1/workloads/{pub['id']}", headers=as_('bob'), json={'title': 'Edited', 'auto_update': True}).status_code == 200
    assert client.post(f"/v1/workloads/{pub['id']}/sync", headers=as_('bob')).status_code == 200
    for patch in ({'execution_mode': 'fixed'}, {'run_as': ''}, {'secrets': {}}, {'env': {}}):
        assert client.patch(f"/v1/workloads/{pub['id']}", headers=as_('bob'), json=patch).status_code == 403
    assert client.delete(f"/v1/workloads/{pub['id']}", headers=as_('bob')).status_code == 403
    assert pub['service_principal_id']
    assert pub['execution_mode'] == 'caller'


def test_environment_folders_deploy_same_app_from_independent_branches(client, repo):
    from .test_deploying import git
    # Remove the fixture's broad Executor grant so environment isolation is observable.
    groups = client.get('/v1/rbac/groups', headers=as_('admin')).json()
    broad = next(g for g in groups if g['name'] == 'Deployed executors')
    assert client.delete(f"/v1/rbac/resources/workloads/assignments/{broad['id']}", headers=as_('admin')).status_code == 204
    finance = client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': 'Finance'}).json()
    sales = client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': 'Sales', 'parent_id': finance['id']}).json()
    folders, apps = {}, {}
    for env in ('tst', 'acp', 'prd'):
        git('push', '-q', 'origin', f'HEAD:refs/heads/{env}', cwd=repo['work'])
        folder = client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': env, 'parent_id': sales['id']})
        assert folder.status_code == 201, folder.text
        folders[env] = folder.json()
        response = deploy(client, repo, program='sales-dashboard', slug=f'sales-{env}', title='Sales dashboard',
                           branch=env, folder_id=folders[env]['id'])
        assert response.status_code == 201, response.text
        apps[env] = response.json()
        assert apps[env]['folder_id'] == folders[env]['id'] and apps[env]['branch'] == env
    assert len({p['service_principal_id'] for p in apps.values()}) == 3

    testers = grant_group(client, folders['tst']['id'], 'Sales testers', 'Executor', users=['bob'])
    acceptance = grant_group(client, folders['acp']['id'], 'Acceptance admins', 'Admin', users=['bob'])
    assert client.put('/v1/me/active-group',headers=as_('bob'),json={'group_id':acceptance}).status_code==200
    visible = client.get('/v1/workloads', headers=as_('bob')).json()
    assert {p['id'] for p in visible} == {apps['acp']['id']}
    grants = client.get(f"/v1/rbac/resources/{apps['tst']['resource_id']}/assignments", headers=as_('bob')).json()
    assert any(g['app_group_id'] == testers and g['inherited'] for g in grants)
    assert client.post(f"/v1/workloads/{apps['tst']['id']}/open", headers=as_('bob')).status_code == 200
    assert client.patch(f"/v1/workloads/{apps['tst']['id']}", headers=as_('bob'), json={'title': 'No'}).status_code == 403
    child = client.post('/v1/rbac/folders', headers=as_('bob'), json={'name': 'Reports', 'parent_id': folders['acp']['id']})
    assert child.status_code == 201 and 'assign' in child.json()['actions']  # The active creating group owns the new child.
    assert client.put(f"/v1/rbac/resources/{child.json()['id']}/assignments", headers=as_('bob'),
                      json={'app_group_id': testers, 'role': 'Owner'}).status_code == 200
    assert client.post('/v1/rbac/folders', headers=as_('bob'), json={'name': 'No', 'parent_id': folders['prd']['id']}).status_code == 403

    assert client.delete(f"/v1/rbac/resources/{folders['tst']['id']}/assignments/{testers}", headers=as_('ana')).status_code == 204
    assert client.post(f"/v1/workloads/{apps['tst']['id']}/open", headers=as_('bob')).status_code == 404
    assert {p['id'] for p in client.get('/v1/workloads', headers=as_('bob')).json()} == {apps['acp']['id']}


def test_revocation_preserves_running_scope_not_new_admission(client, monkeypatch):
    setup_finance_db(client)
    ws = start_workspace(client)
    run = client.post('/v1/runs', headers=as_('ana'), json={'workspace_id': ws, 'pool': 'small', 'kind': 'script', 'entrypoint': 'x.py'}).json()
    spec = deps.get_orchestrator().executions[f"run-{run['id']}"]
    with SessionLocal() as db:
        ds = db.scalar(select(DataSource).where(DataSource.name == 'finance-dw'))
        schema = db.scalar(select(Resource).where(Resource.kind == 'schema', Resource.object_id == ds.id))
        group = db.scalar(select(AppGroup).where(AppGroup.name == 'Finance data'))
        sid, gid = schema.id, group.id
    assert client.delete(f'/v1/rbac/resources/{sid}/assignments/{gid}', headers=as_('admin')).status_code == 204
    from app import data_access
    seen = []
    def fake_credentials(db, ds, scope, *args):
        seen.append(scope)
        return {'username': 'scoped', 'password': 'test'}, {'schemas': scope}
    monkeypatch.setattr(data_access, 'credentials', fake_credentials)
    bearer = {'Authorization': f"Bearer {spec.env['FDP_SESSION_TOKEN']}"}
    assert client.get('/v1/connect/finance-dw', headers=bearer).status_code == 200
    assert seen == [{'public': 'Reader'}]
    _, interactive = execution_token(ws)
    assert client.get('/v1/connect/finance-dw', headers=interactive).status_code == 404
    assert client.post('/v1/orchestrate/runs', headers=bearer, json={'workload': 'not-there'}).status_code == 404
    client.post(f"/v1/runs/{run['id']}/stop", headers=as_('ana'))
    assert client.get('/v1/connect/finance-dw', headers=bearer).status_code == 401


def test_native_credentials_fail_closed_until_provisioned(client):
    setup_finance_db(client)
    _, bearer = execution_token(start_workspace(client))
    assert client.get('/v1/connect/finance-dw', headers=bearer).status_code == 409
    assert client.post('/v1/admin/datasources/finance-dw/grants', headers=as_('admin'), json={'group': 'finance'}).status_code == 410


def test_scim_memberships_override_stale_claims_and_disable_open_sessions(client, repo):
    from app.config import get_settings
    get_settings().scim_token = 'provision-test'
    scim = {'Authorization': 'Bearer provision-test'}
    assert client.get('/scim/v2/Users').status_code == 401
    # Adopt Ana's existing stable directory identity and bind explicit SCIM membership.
    ana = client.post('/scim/v2/Users', headers=scim, json={'userName': 'ana', 'externalId': 'ana-id', 'roles': []})
    assert ana.status_code == 201, ana.text
    aid = ana.json()['id']
    group = client.post('/scim/v2/Groups', headers=scim, json={'displayName': 'finance', 'externalId': 'finance-id', 'members': [{'value': aid}]}).json()
    # Existing group binding survives OIDC -> SCIM adoption.
    assert client.get('/v1/me', headers=as_('ana')).json()['data_sources']
    pub = deploy(client, repo, program='sales-dashboard', slug='app').json()
    uri = urlsplit(client.post(f"/v1/workloads/{pub['id']}/open", headers=as_('ana')).json()['url'])
    response = client.get('/internal/auth/forward', headers={'X-Forwarded-Uri': f'{uri.path}?{uri.query}'}, follow_redirects=False)
    cookie = response.cookies['dc_p_app']
    change = client.patch(f"/scim/v2/Groups/{group['id']}", headers=scim,
                          json={'Operations': [{'op': 'remove', 'path': f'members[value eq "{aid}"]'}]})
    assert change.status_code == 200
    assert client.get('/v1/me', headers=as_('ana')).json()['data_sources'] == []
    assert client.patch(f'/scim/v2/Users/{aid}', headers=scim, json={'Operations': [{'op': 'replace', 'path': 'active', 'value': False}]}).status_code == 200
    assert client.get('/v1/workloads', headers=as_('ana')).status_code == 401
    assert client.get('/internal/auth/forward', headers={'X-Forwarded-Uri': '/p/app/'}, cookies={'dc_p_app': cookie}).status_code == 403


def test_scim_nested_cycle_filter_rename_and_atomic_patch(client):
    from app.config import get_settings
    get_settings().scim_token = 'provision-test'
    h = {'Authorization': 'Bearer provision-test'}
    u = client.post('/scim/v2/Users', headers=h, json={'userName': 'new-user', 'externalId': 'immutable-id'}).json()
    a = client.post('/scim/v2/Groups', headers=h, json={'displayName': 'A', 'members': [{'value': u['id']}]}).json()
    b = client.post('/scim/v2/Groups', headers=h, json={'displayName': 'B', 'members': [{'value': a['id']}]}).json()
    assert client.patch(f"/scim/v2/Groups/{a['id']}", headers=h, json={'Operations': [{'op': 'add', 'path': 'members', 'value': [{'value': b['id']}]}]}).status_code == 400
    assert client.patch(f"/scim/v2/Users/{u['id']}", headers=h, json={'Operations': [
        {'op': 'replace', 'path': 'userName', 'value': 'renamed'}, {'op': 'replace', 'path': 'notAnAttribute', 'value': 'bad'}]}).status_code == 400
    assert client.get(f"/scim/v2/Users/{u['id']}", headers=h).json()['userName'] == 'new-user'
    renamed = client.put(f"/scim/v2/Users/{u['id']}", headers=h, json={'userName': 'renamed', 'externalId': 'immutable-id'})
    assert renamed.status_code == 200 and renamed.json()['id'] == u['id']
    page = client.get('/scim/v2/Users', headers=h, params={'filter': 'userName eq "renamed"', 'startIndex': 1, 'count': 1}).json()
    assert page['totalResults'] == 1 and page['Resources'][0]['id'] == u['id']
    assert client.get('/scim/v2/Users', headers=h, params={'filter': 'unknown co "x"'}).status_code == 400


def test_service_execution_right_does_not_allow_tokens_or_secrets(client):
    from .test_platform import make_sa
    setup_finance_db(client)
    make_sa(client)
    resources = client.get('/v1/rbac/resources', headers=as_('admin')).json()
    account = next(r for r in resources if r['config'].get('service_account'))
    grant_group(client, account['id'], 'Service callers', 'Executor', users=['bob'])
    assert client.get('/v1/service-accounts', headers=as_('bob')).json()[0]['name'] == 'finance-etl'
    assert client.post('/v1/service-accounts/finance-etl/tokens', headers=as_('bob'), json={'name': 'not-allowed'}).status_code == 403
    assert client.put('/v1/secrets/sa/finance-etl/not-allowed', headers=as_('bob'), json={'value': 'no'}).status_code == 403
    assert client.get('/v1/service-accounts/finance-etl/tokens', headers=as_('bob')).status_code == 403


def test_only_an_active_job_can_renew_expired_context(client):
    setup_finance_db(client)
    ws = start_workspace(client)
    run = client.post('/v1/runs', headers=as_('ana'), json={'workspace_id': ws, 'pool': 'small', 'kind': 'script', 'entrypoint': 'x.py'}).json()
    expired = mint_token('execution', 'ana', -1, run=run['id'], ws=ws)
    response = client.post('/v1/executions/token', headers={'Authorization': 'Bearer ' + expired})
    assert response.status_code == 200
    assert read_token(response.json()['token'], 'execution')['run'] == run['id']
    client.post(f"/v1/runs/{run['id']}/stop", headers=as_('ana'))
    assert client.post('/v1/executions/token', headers={'Authorization': 'Bearer ' + expired}).status_code == 401
    workspace_only = mint_token('execution', 'ana', -1, ws=ws)
    assert client.post('/v1/executions/token', headers={'Authorization': 'Bearer ' + workspace_only}).status_code == 401


def test_scim_malformed_payloads_are_errors_not_server_failures(client):
    from app.config import get_settings
    get_settings().scim_token = 'provision-test'
    h = {'Authorization': 'Bearer provision-test', 'Content-Type': 'application/scim+json'}
    bad_json = client.post('/scim/v2/Users', headers=h, content='not-json')
    assert bad_json.status_code == 400 and bad_json.json()['status'] == '400'
    user = client.post('/scim/v2/Users', headers=h, json={'userName': 'test-user'}).json()
    response = client.patch(f"/scim/v2/Users/{user['id']}", headers=h, json={'Operations': ['invalid']})
    assert response.status_code == 400
    discovery = client.get('/scim/v2/Schemas/urn:ietf:params:scim:schemas:core:2.0:User', headers=h)
    assert discovery.status_code == 200 and discovery.headers['content-type'].startswith('application/scim+json')


def test_admin_cannot_gain_owner_by_moving_a_resource(client):
    setup_finance_db(client)
    a = client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': 'Protected'}).json()
    b = client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': 'Bob owns this'}).json()
    child = client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': 'Item', 'parent_id': a['id']}).json()
    grant_group(client, a['id'], 'Bob source admins', 'Admin', users=['bob'])
    grant_group(client, b['id'], 'Bob destination owners', 'Owner', users=['bob'])
    move = client.patch(f"/v1/rbac/resources/{child['id']}", headers=as_('bob'), json={'name': 'Item', 'parent_id': b['id']})
    assert move.status_code == 403
    assert client.patch(f"/v1/rbac/resources/{child['id']}", headers=as_('ana'), json={'name': 'Item', 'parent_id': b['id']}).status_code == 200


def test_strict_scim_directory_rejects_unprovisioned_claim_backed_users(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'directory_mode', 'scim')
    assert client.get('/v1/me', headers=as_('ana')).status_code == 401
    monkeypatch.setattr(get_settings(), 'scim_token', 'strict-scim')
    p = client.post('/scim/v2/Users', headers={'Authorization': 'Bearer strict-scim'},
                    json={'userName': 'ana', 'roles': [], 'active': True})
    assert p.status_code == 201
    assert client.get('/v1/me', headers=as_('ana')).status_code == 200
    with SessionLocal() as db:
        first = rbac.resolve_user(db, User('ana', subject='stable-subject'), refresh=True)
        pid = first.principal_id
        db.commit()
    with SessionLocal() as db:
        import pytest
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            rbac.resolve_user(db, User('ana', subject='different-subject'), refresh=True)
        assert db.get(DirectoryPrincipal, pid).subject == 'stable-subject'


def test_reused_username_does_not_take_private_workspaces_or_runs(client, monkeypatch):
    from .conftest import USERS
    monkeypatch.setattr(USERS['ana'], 'subject', 'original-subject')
    ws = start_workspace(client, 'ana')
    run = client.post('/v1/runs', headers=as_('ana'), json={'workspace_id': ws, 'pool': 'small', 'kind': 'script', 'entrypoint': 'x.py'}).json()
    monkeypatch.setitem(USERS, 'bob', User('ana', subject='replacement-subject'))
    assert client.get('/v1/workspaces', headers=as_('bob')).json() == []
    assert client.post(f'/v1/workspaces/{ws}/open', headers=as_('bob')).status_code == 404
    assert client.get('/v1/runs', headers=as_('bob')).json() == []
    assert client.get(f"/v1/runs/{run['id']}", headers=as_('bob')).status_code == 404


def test_legacy_workload_migrates_before_scim_provisioning(client, monkeypatch):
    from app.models import Workload, GitRepository
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'directory_mode', 'scim')
    with SessionLocal() as db:
        repo = GitRepository(name='legacy', url='https://example.test/legacy.git')
        db.add(repo)
        db.flush()
        pub = Workload(slug='legacy', title='Legacy', repo_id=repo.id, branch='main', program='job',
                          kind='script', runtime='python', owner='not-provisioned-yet', warehouse='small')
        db.add(pub)
        db.flush()
        resource = rbac.workload_resource(db, pub)
        db.commit()
        assert resource.kind == 'service' and pub.owner_principal_id
        assert not db.get(DirectoryPrincipal, pub.owner_principal_id).managed


def test_global_admin_data_access_and_ordinary_schema_scope(client):
    client.put('/v1/admin/datasources/access-dw', headers=as_('admin'), json=integration('access-dw', 'postgres')).raise_for_status()
    for name in ('granted', 'hidden'):
        client.post('/v1/rbac/sources/access-dw/schemas', headers=as_('admin'), json={'name': name}).raise_for_status()
    rows = lambda user: {(r['kind'], r['name']): r for r in client.get('/v1/rbac/resources', headers=as_(user)).json()
                         if r['kind'] in ('source', 'schema') and (r['name'] == 'access-dw' or r['parent_id'] == source_id)}
    source_id = next(r['id'] for r in client.get('/v1/rbac/resources', headers=as_('admin')).json() if r['name'] == 'access-dw')
    # Global Admin is protected and includes data access, independently of ordinary grants.
    assert client.delete(f'/v1/rbac/resources/{source_id}/assignments/000000000001',headers=as_('admin')).status_code == 409
    admin = rows('admin')
    assert admin[('source', 'access-dw')]['data_access'] is True and 'assign' in admin[('source', 'access-dw')]['actions']
    assert admin[('schema', 'granted')]['data_access'] is True
    grant_group(client, admin[('schema', 'granted')]['id'], 'Access readers', 'Reader', users=['bob'])
    bob = rows('bob')
    assert bob[('schema', 'granted')]['data_access'] is True
    # The source is listed only to navigate to the granted schema; the other schema is not listed.
    assert bob[('source', 'access-dw')]['data_access'] is False and ('schema', 'hidden') not in bob
    selected=grant_group(client, source_id, 'Access source readers', 'Reader', users=['bob'])
    assert client.put('/v1/me/active-group',headers=as_('bob'),json={'group_id':selected}).status_code==200
    bob = rows('bob')
    assert bob[('source', 'access-dw')]['data_access'] is True and bob[('schema', 'hidden')]['data_access'] is True
