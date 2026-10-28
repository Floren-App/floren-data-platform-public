"""Direct managed-identity grants use the same policy as group grants, without group creation."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app import catalog, data_access, rbac, sql_access
from app.auth import User
from app.db import SessionLocal
from app.models import AppGroup, DataSource, IdentityBinding, Resource, Run, ServiceRoleAssignment
from .conftest import as_
from .rbac_helpers import grant_group
from .test_api import setup_finance_db
from .test_deploying import deploy, repo  # noqa: F401
from .test_shared_sql import sql_mode  # noqa: F401


def service_account(client):
    response = client.put('/v1/admin/service-accounts/direct-etl', headers=as_('admin'), json={})
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        actor = rbac.resolve_user(db, User('sa:direct-etl'))
        db.commit()
        return actor


def assign(client, resource_id, principal_id, role, user='admin'):
    return client.put(f'/v1/rbac/resources/{resource_id}/assignments', headers=as_(user),
                      json={'principal_id': principal_id, 'role': role})


def test_direct_service_data_roles_reach_trino_catalog_and_snapshots_without_a_group(sql_mode):
    client, _ = sql_mode
    actor = service_account(client)
    with SessionLocal() as db:
        source = db.scalar(select(DataSource).where(DataSource.name == 'finance'))
        source_id = source.id
        parent = rbac.source_resource(db, source).id
        schema_id = db.scalar(select(Resource.id).where(Resource.parent_id == parent))
        group_count = db.scalar(select(func.count()).select_from(AppGroup))
        assert sql_access.scopes(db, sql_access.SqlActor(actor, 'test')) == {}
    assert assign(client, parent, actor.principal_id, 'Contributor').status_code == 200
    assert assign(client, schema_id, actor.principal_id, 'Reader').status_code == 200
    with SessionLocal() as db:
        source = db.get(DataSource, source_id)
        assert rbac.data_scope(db, actor, source) == {'reporting': 'Contributor'}
        assert sql_access.scopes(db, sql_access.SqlActor(actor, 'test'))['finance']['schemas'] == {'reporting': 'Contributor'}
        assert data_access.snapshot(db, actor)[source.id] == {'reporting': 'Contributor'}
        _, actions = catalog.permissions(db, actor)
        assert {'read', 'write'} <= actions[schema_id] and 'create' not in actions[schema_id]
        assert db.scalar(select(func.count()).select_from(AppGroup)) == group_count
        assert not db.scalar(select(IdentityBinding.id).where(IdentityBinding.principal_id == actor.principal_id))
    # A stronger group grant still adds to a weaker direct grant.
    grant_group(client, schema_id, 'Extra DDL', 'Admin', services=[actor.principal_id])
    with SessionLocal() as db:
        assert rbac.data_scope(db, actor, db.get(DataSource, source_id)) == {'reporting': 'Admin'}
    assert client.delete(f'/v1/rbac/resources/{parent}/assignments/services/{actor.principal_id}', headers=as_('admin')).status_code == 204
    with SessionLocal() as db:
        assert rbac.data_scope(db, actor, db.get(DataSource, source_id)) == {'reporting': 'Admin'}


def test_registered_workload_identity_can_be_granted_data_directly(client, repo):
    pub = deploy(client, repo, program='sales-dashboard', slug='direct-dashboard', execution_mode='fixed').json()
    assert pub['status'] == 'failed'
    resources = client.get('/v1/rbac/resources', headers=as_('admin')).json()
    schema = next(r for r in resources if r['kind'] == 'schema' and r['name'] == 'public')
    pid = pub['service_principal_id']
    # A schema Owner can use the service picker without being an IdP directory admin/group owner.
    picked = client.get(f"/v1/rbac/resources/{schema['id']}/service-principals", headers=as_('admin')).json()
    assert any(p['id'] == pid and p['name'] == pub['title'] for p in picked)
    first = assign(client, schema['id'], pid, 'Reader')
    assert first.status_code == 200, first.text
    second = assign(client, schema['id'], pid, 'Contributor')
    assert second.status_code == 200
    grants = [g for g in second.json() if g['principal_id'] == pid]
    assert len(grants) == 1 and grants[0]['role'] == 'Contributor' and grants[0]['app_group_id'] is None
    repaired = client.post(f"/v1/workloads/{pub['id']}/sync", headers=as_('ana')).json()
    assert repaired['status'] == 'live' and repaired['access_issue'] is None
    with SessionLocal() as db:
        assert not db.scalar(select(IdentityBinding.id).where(IdentityBinding.principal_id == pid))


def test_direct_grants_require_resource_owner_and_only_accept_active_users_or_services(client):
    setup_finance_db(client)
    actor = service_account(client)
    schema = next(r for r in client.get('/v1/rbac/resources', headers=as_('admin')).json() if r['kind'] == 'schema')
    url = f"/v1/rbac/resources/{schema['id']}/service-principals"
    assert client.get(url, headers=as_('ana')).status_code == 403
    assert assign(client, schema['id'], actor.principal_id, 'Owner', 'ana').status_code == 403
    assert client.delete(f"/v1/rbac/resources/{schema['id']}/assignments/services/{actor.principal_id}", headers=as_('ana')).status_code == 403
    directory = client.get('/v1/rbac/directory', headers=as_('admin')).json()
    human = next(p for p in directory if p['kind'] == 'user')
    assert assign(client, schema['id'], human['id'], 'Reader').status_code == 200
    assert assign(client, schema['id'], 'missing', 'Reader').status_code == 422
    assert assign(client, schema['id'], actor.principal_id, 'Executor').status_code == 422
    assert client.put(f"/v1/rbac/resources/{schema['id']}/assignments", headers=as_('admin'),
                      json={'principal_id': actor.principal_id, 'app_group_id': 'also-set', 'role': 'Reader'}).status_code == 422
    # An object Owner need not be a group owner to discover service identities.
    grant_group(client, schema['id'], 'Schema owners', 'Owner', users=['bob'])
    assert client.get('/v1/rbac/directory', headers=as_('bob')).status_code == 403
    assert client.get(url, headers=as_('bob')).status_code == 200
    assert assign(client, schema['id'], actor.principal_id, 'Reader', 'bob').status_code == 200
    assert client.delete('/v1/admin/service-accounts/direct-etl', headers=as_('admin')).status_code == 204
    assert actor.principal_id not in {p['id'] for p in client.get(url, headers=as_('admin')).json()}
    assert assign(client, schema['id'], actor.principal_id, 'Reader').status_code == 422
    with SessionLocal() as db, pytest.raises(HTTPException):
        rbac.resolve_user(db, User('service:' + actor.principal_id))


def test_revocation_blocks_new_jobs_but_retains_running_job_snapshot(client, repo):
    pub = deploy(client, repo, program='sales-report', slug='direct-report', execution_mode='fixed').json()
    pid = pub['service_principal_id']
    schema = next(r for r in client.get('/v1/rbac/resources', headers=as_('admin')).json() if r['kind'] == 'schema')
    assert assign(client, schema['id'], pid, 'Contributor').status_code == 200
    first = client.post(f"/v1/workloads/{pub['id']}/runs", headers=as_('ana'), json={})
    assert first.status_code == 201, first.text
    with SessionLocal() as db:
        run = db.get(Run, first.json()['id'])
        snapshot = run.access_snapshot
        assert snapshot[schema['object_id']]['public'] == 'Contributor'
    assert client.delete(f"/v1/rbac/resources/{schema['id']}/assignments/services/{pid}", headers=as_('admin')).status_code == 204
    assert client.post(f"/v1/workloads/{pub['id']}/runs", headers=as_('ana'), json={}).status_code == 403
    with SessionLocal() as db:
        assert db.get(Run, first.json()['id']).access_snapshot == snapshot


def test_service_inheritance_cannot_be_changed_by_folder_move_without_owner(client):
    setup_finance_db(client)
    actor = service_account(client)
    folders = [client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': n, 'layer_id': '1a0000000002'}).json() for n in ('A', 'B')]
    child = client.post('/v1/rbac/folders', headers=as_('ana'), json={'name': 'Child', 'parent_id': folders[0]['id']}).json()
    for folder in folders:
        grant_group(client, folder['id'], 'Folder admins', 'Admin', users=['bob'])
    assert assign(client, folders[1]['id'], actor.principal_id, 'Owner').status_code == 200
    assert client.patch(f"/v1/rbac/resources/{child['id']}", headers=as_('bob'), json={'name': 'Child', 'parent_id': folders[1]['id']}).status_code == 403
    assert client.patch(f"/v1/rbac/resources/{child['id']}", headers=as_('ana'), json={'name': 'Child', 'parent_id': folders[1]['id']}).status_code == 200
    inherited = client.get(f"/v1/rbac/resources/{child['id']}/assignments", headers=as_('ana')).json()
    assert any(g['principal_id'] == actor.principal_id and g['inherited'] for g in inherited)
    # Deleting a resource cleans direct grants too (also exercised against PostgreSQL FKs).
    assert assign(client, child['id'], actor.principal_id, 'Executor').status_code == 200
    assert client.delete(f"/v1/rbac/folders/{child['id']}", headers=as_('ana')).status_code == 204
    with SessionLocal() as db:
        assert not db.scalar(select(ServiceRoleAssignment.id).where(ServiceRoleAssignment.resource_id == child['id']))
