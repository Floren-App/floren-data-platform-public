"""Persona journeys: each kind of person completes their intended workflow, and is refused what they should not do.

These are the release gate for workflow and permission-model changes. Each persona gets its access the way an
installation's Terraform gives it: reusable policies on collections and layers, never a grant on one workspace ID.
Every journey uses fresh IDs and, where an editor is involved, the editor's own credential as well as the browser's.
Run them alone with `make persona-acceptance`, which also writes a report.
"""
import pytest
from sqlalchemy import select

from app.auth import mint_token
from app.db import SessionLocal
from app.models import AppGroup
from .conftest import as_
from .rbac_helpers import git_permissions, grant_group, secret_permissions, select_group, workspace_permissions
from .test_classification_options import classify
from .test_compliance import ANSWERS, can_read, set_up_policy
from .test_deploying import repo  # noqa: F401
from .test_products import setup_products

pytestmark = pytest.mark.persona
RUN = {'pool': 'small', 'kind': 'script', 'entrypoint': 'job.py'}


def group_for(client, name, user):
    """An app group with one member, selected as that person's acting group; no access yet."""
    gid = grant_group(client, 'workloads', name, 'Reader', users=[user])
    select_group(client, user, gid)
    return gid


def policy(client, gid, namespace, *roles):
    """A reusable policy: roles on a whole collection, in every layer, the way Terraform assigns them."""
    for role in roles:
        response = client.post('/v1/permissions/assignments', headers=as_('admin'), json={
            'target_id': 'workspaces', 'namespace': namespace, 'layer_id': '*', 'app_group_id': gid, 'role': role})
        assert response.status_code == 201, response.text


def new_workspace(client, user, name):
    response = client.post('/v1/workspaces', headers=as_(user), json={'name': name, 'editor': 'vscode'})
    assert response.status_code == 201, response.text
    ws = response.json()['id']
    assert client.post(f'/v1/workspaces/{ws}/start', headers=as_(user)).json()['status'] == 'running'
    return ws


def readiness(client, ws, headers):
    response = client.get(f'/v1/workspaces/{ws}/readiness', headers=headers)
    assert response.status_code == 200, response.text
    return {c['key']: c['state'] for c in response.json()['checks']}


def create_group(client, headers, user):
    """Creating an app group: platform administration."""
    owner = client.get('/v1/me', headers=as_(user)).json()['principal_id']
    return client.post('/v1/rbac/groups', headers=headers, json={'name': f'Made by {user}', 'owners': [owner]}).status_code


def editor(user, ws):
    return {'Authorization': 'Bearer ' + mint_token('execution', user, 3600, ws=ws)}


def test_engineer_works_in_fresh_workspaces_from_a_reusable_policy(client):
    gid = group_for(client, 'Engineers', 'ana')
    secret_permissions(client, 'ana'); git_permissions(client, 'ana')
    workspace_permissions(client, gid)
    first, second = new_workspace(client, 'ana', 'first'), new_workspace(client, 'ana', 'second')  # no per-ID repairs
    for ws in (first, second):
        state = readiness(client, ws, as_('ana'))
        assert state['runs'] == state['logs'] == state['outputs'] == 'ready', state
        assert readiness(client, ws, editor('ana', ws))['runs'] == 'ready'
    run = client.post('/v1/runs', headers=editor('ana', first), json={'workspace_id': first, **RUN})
    assert run.status_code == 201, run.text
    assert client.get(f"/v1/runs/{run.json()['id']}/logs", headers=editor('ana', first)).status_code == 200
    assert client.post(f"/v1/runs/{run.json()['id']}/stop", headers=as_('ana')).status_code == 200
    # Refused: another workspace through this editor, someone else's workspace, and changing policy.
    other = client.post('/v1/runs', headers=as_('ana'), json={'workspace_id': second, **RUN}).json()['id']
    assert client.get(f'/v1/runs/{other}', headers=editor('ana', first)).status_code == 404
    assert client.get(f'/v1/workspaces/{first}/readiness', headers=as_('bob')).status_code == 404
    assert client.post('/v1/permissions/assignments', headers=as_('ana'), json={
        'target_id': 'workspaces', 'namespace': 'runs', 'layer_id': '*', 'app_group_id': gid, 'role': 'Owner'}).status_code == 403


def test_analyst_runs_work_without_reading_its_logs(client):
    """An Executor may run work whose output it is not meant to read; readiness says so before it tries."""
    gid = group_for(client, 'Analysts', 'bob')
    secret_permissions(client, 'bob'); git_permissions(client, 'bob')
    policy(client, gid, 'workspaces', 'Contributor', 'Executor', 'DataReader', 'Admin')
    policy(client, gid, 'runs', 'Executor')
    ws = new_workspace(client, 'bob', 'analysis')
    state = readiness(client, ws, as_('bob'))
    assert state['runs'] == 'ready' and state['logs'] == state['outputs'] == 'missing', state
    run = client.post('/v1/runs', headers=as_('bob'), json={'workspace_id': ws, **RUN})
    assert run.status_code == 201, run.text
    assert client.get(f"/v1/runs/{run.json()['id']}/logs", headers=as_('bob')).status_code == 403
    assert client.get(f"/v1/runs/{run.json()['id']}/outputs", headers=as_('bob')).status_code == 403


def test_viewer_requests_for_the_acting_group_and_uses_the_product_once_approved(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    product = client.post('/v1/catalog/products', headers=as_('ana'), json={'asset_ids': [ids['sales']], 'title': 'Sales'}).json()
    acting = client.get('/v1/me', headers=as_('bob')).json()['active_group_id']
    with SessionLocal() as db:
        finance = db.scalar(select(AppGroup).where(AppGroup.name == 'Finance data')).id
    # Discovers the product; nothing in it before approval.
    assert [p['id'] for p in client.get('/v1/catalog/products', headers=as_('bob')).json()['items']] == [product['id']]
    data = client.get(f"/v1/catalog/resources/{ids['public']}/asset", headers=as_('bob'), params={'object_path': 'sales'})
    assert data.status_code == 404
    # Requests only for the group it acts as.
    url = f"/v1/catalog/products/{product['id']}/requests"
    assert client.post(url, headers=as_('bob'), json={'app_group_id': finance, 'reason': 'not my group'}).status_code == 422
    request = client.post(url, headers=as_('bob'), json={'app_group_id': acting, 'reason': 'Weekly report'})
    assert request.status_code == 201, request.text
    assert client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_('ana'), json={'approve': True}).status_code == 200
    assert client.get(f"/v1/catalog/resources/{ids['public']}/asset", headers=as_('bob'), params={'object_path': 'sales'}).status_code == 200
    # Never administration.
    assert create_group(client, as_('bob'), 'bob') == 403


def test_privacy_reviewer_decides_cases_in_scope_without_seeing_the_data(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    classify(client, ids['sales'], {'email': {'classification': 'restricted', 'tags': ['pii']}})
    layer, _ = set_up_policy(client, ids['public'])
    readers = grant_group(client, 'workloads', 'Report readers', 'Reader')
    privacy = group_for(client, 'Privacy office', 'bob')
    assert client.post('/v1/compliance/roles', headers=as_('admin'), json={'role': 'compliance_reviewer', 'app_group_id': privacy,
                       'scope': {'layers': [layer], 'classifications': ['restricted']}}).status_code == 201
    target = {'kind': 'role', 'resource_id': ids['public'], 'role': 'DataReader', 'app_group_id': readers}
    case = client.post('/v1/compliance/cases', headers=as_('ana'), json={**target, 'answers': ANSWERS}).json()
    assert [c['id'] for c in client.get('/v1/compliance/cases', headers=as_('bob'), params={'box': 'review'}).json()] == [case['id']]
    # A reviewer duty is not data access, and not platform administration.
    assert not can_read('bob', ids['public'])
    assert create_group(client, as_('bob'), 'bob') == 403
    assert client.post(f"/v1/compliance/cases/{case['id']}/decision", headers=as_('bob'), json={'decision': 'approve'}).json()['status'] == 'approved'
    assert not can_read('bob', ids['public'])


def test_global_admin_authority_stays_in_the_browser(client):
    """Administration is explicit and interactive; an administrator's editor and jobs carry none of it."""
    assert create_group(client, as_('admin'), 'admin') == 201
    ws = client.post('/v1/workspaces', headers=as_('admin'), json={'name': 'admin-ws', 'editor': 'vscode'}).json()['id']
    client.post(f'/v1/workspaces/{ws}/start', headers=as_('admin'))
    assert create_group(client, editor('admin', ws), 'admin') == 403
    # Nor does an administrator's editor reach other people's runs.
    gid = group_for(client, 'Engineers', 'ana')
    secret_permissions(client, 'ana'); git_permissions(client, 'ana')
    workspace_permissions(client, gid)
    theirs = new_workspace(client, 'ana', 'theirs')
    run = client.post('/v1/runs', headers=as_('ana'), json={'workspace_id': theirs, **RUN}).json()['id']
    assert client.get(f'/v1/runs/{run}', headers=as_('admin')).status_code == 200
    assert client.get(f'/v1/runs/{run}', headers=editor('admin', ws)).status_code == 404
