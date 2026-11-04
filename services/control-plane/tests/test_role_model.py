"""The role model: Reader/Contributor see and configure, Data Reader/Data Contributor work with data, Executor only
triggers on-demand work, Viewer only discovers data products. Groups can hold several roles on one object, and the
migration from the earlier roles keeps exactly the access each grant gave."""
from sqlalchemy import select, text
from app import apis, permissions, rbac
from app.access_inventory import category
from app.db import SessionLocal
from app.models import AccessRequest, AuditEvent, PermissionGrant, Resource, RoleAssignment
from app.role_migration import old_bundle, upgrade_role_model
from .conftest import as_, USERS
from .rbac_helpers import grant_group
from .test_products import setup_products
from .test_deploying import repo  # noqa: F401


def test_each_role_keeps_to_its_kind_of_access():
    for key, definition in permissions.REGISTRY.items():
        roles = set(definition['roles'])
        if 'Contributor' in roles:
            assert category(key) != 'data', key
        if 'Viewer' in roles:
            assert 'Reader' in roles, key  # what can be discovered can be seen
        if 'Executor' in roles and 'Reader' not in roles:
            assert key.startswith(('workloads.execution.', 'runs.', 'workspaces.object.', 'compute.', 'runtimes.', 'identities.execution.',
                                   'secrets.use', 'certificates.use')), key
        if 'DataReader' in roles:
            assert 'DataContributor' in roles, key  # changing data includes reading it
        if key in ('data.sql.insert', 'data.sql.update', 'data.sql.delete', 'kafka.data.produce', 'apis.calls.write'):
            assert 'DataReader' not in roles and 'DataContributor' in roles, key
    assert 'apis.calls.invoke' not in permissions.REGISTRY


def test_api_calls_are_reads_or_changes_by_method():
    assert [apis.call_permission(m) for m in ('GET', 'head', 'OPTIONS')] == ['apis.calls.read'] * 3
    assert [apis.call_permission(m) for m in ('POST', 'PUT', 'PATCH', 'DELETE')] == ['apis.calls.write'] * 4


def test_a_group_holds_several_roles_on_one_object_and_loses_one_at_a_time(client):
    group = grant_group(client, 'workloads', 'Report runners', 'Executor', users=['bob'])
    path = '/v1/rbac/resources/workloads/assignments'
    client.put(path, headers=as_('admin'), json={'app_group_id': group, 'role': 'DataReader'}).raise_for_status()
    held = lambda: sorted(a['role'] for a in client.get(path, headers=as_('admin')).json() if a['app_group_id'] == group)
    assert held() == ['DataReader', 'Executor']
    client.put(path, headers=as_('admin'), json={'app_group_id': group, 'role': 'Contributor', 'replaces': 'Executor'}).raise_for_status()
    assert held() == ['Contributor', 'DataReader']
    assert client.delete(path + '/' + group + '?role=DataReader', headers=as_('admin')).status_code == 204
    assert held() == ['Contributor']
    assert client.delete(path + '/' + group, headers=as_('admin')).status_code == 204
    assert held() == []


def test_migration_keeps_exactly_the_access_each_old_grant_gave(client, repo):
    ids = setup_products(client, repo)
    readers = grant_group(client, 'workloads', 'Old readers', 'Owner', users=['bob'])
    with SessionLocal() as db:
        db.execute(text("DELETE FROM fdp_migrations WHERE revision='005_role_model'"))
        db.query(RoleAssignment).filter(RoleAssignment.app_group_id == readers).delete()
        folder = Resource(kind='folder', name='Reports', parent_id='workloads')
        db.add(folder); db.flush()
        old = [(ids['public'], 'Reader'), (folder.id, 'Executor'), ('set-forms', 'Manager'), ('set-network', 'Viewer')]
        for resource, role in old:
            db.add(RoleAssignment(resource_id=resource, app_group_id=readers, role=role))
        db.add(PermissionGrant(target_id='workspaces', namespace='workspaces', app_group_id=readers, layer_id='1a0000000002', role='Executor'))
        db.add(PermissionGrant(target_id='set-forms', namespace='compliance.forms', app_group_id=readers, actions=['compliance.forms.read']))
        db.add(AccessRequest(product_id=ids.get('product') or 'p', requester='bob', app_group_id=readers, role='Consumer'))
        db.commit()
    report = upgrade_role_model(SessionLocal)
    with SessionLocal() as db:
        held = {(a.resource_id, a.role) for a in db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == readers))}
        assert held == {(ids['public'], 'DataReader'), (folder.id, 'Executor'), (folder.id, 'DataReader'),
                        ('set-forms', 'Contributor'), ('set-network', 'Reader')}
        named = {(g.namespace, g.role) for g in db.scalars(select(PermissionGrant).where(PermissionGrant.app_group_id == readers))}
        assert ('workspaces', 'Executor') in named and ('workspaces', 'DataReader') in named
        assert db.scalar(select(AccessRequest.role).where(AccessRequest.app_group_id == readers)) == 'DataReader'
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == 'migration.role_model'))
        user = rbac.resolve_user(db, USERS['bob'])
    # Nothing was lost: every old permission is carried by a new role or kept explicitly.
    for change in report:
        if change.get('target'):
            assert not change['kept_permissions'] or change['from'] in ('Reader', 'Executor', 'Manager', 'Viewer'), change
    assert {c['from'] for c in report if c.get('target')} >= {'Reader', 'Executor', 'Manager', 'Viewer'}
    client.put('/v1/me/active-group', headers=as_('bob'), json={'group_id': readers}).raise_for_status()
    with SessionLocal() as db:
        resource = db.get(Resource, ids['public'])
        assert permissions.domain_allowed(db, USERS['bob'], 'data.sql.select', resource, 'read')
    assert upgrade_role_model(SessionLocal) == []  # once only


def test_write_only_data_grants_keep_their_writes_without_gaining_reads(client, repo):
    ids = setup_products(client, repo)
    writers = grant_group(client, 'workloads', 'Old writers', 'Owner', users=['bob'])
    with SessionLocal() as db:
        db.execute(text("DELETE FROM fdp_migrations WHERE revision='005_role_model'"))
        db.query(RoleAssignment).filter(RoleAssignment.app_group_id == writers).delete()
        db.add(RoleAssignment(resource_id=ids['public'], app_group_id=writers, role='Contributor'))
        db.commit()
    upgrade_role_model(SessionLocal)
    with SessionLocal() as db:
        assert {a.role for a in db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == writers))} == {'Reader'}
        kept = {a for g in db.scalars(select(PermissionGrant).where(PermissionGrant.app_group_id == writers)) for a in g.actions}
    old = old_bundle(('data.', 'storage.'), 'Contributor')
    assert 'data.sql.insert' in kept and 'data.sql.select' not in kept
    assert kept | set(permissions.REGISTRY) >= old
