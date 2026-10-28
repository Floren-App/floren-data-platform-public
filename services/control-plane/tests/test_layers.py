from sqlalchemy import select, text
import pytest

from app import deps, layers, rbac, sql_access
from app.auth import User, mint_token
from app.db import SessionLocal
from app.models import (AppGroup, DataSource, IdentityBinding, Resource, RoleAssignment,
                        ServiceRoleAssignment, Workspace)
from .conftest import as_
from .rbac_helpers import integration


def test_any_person_can_create_a_private_workspace_in_an_enabled_layer_without_groups(client):
    from .test_api import onboard_workspace_group
    onboard_workspace_group(client,'bob')
    initial = client.get('/v1/layers', headers=as_('bob')).json()
    assert {(l['name'], l['workspaces']) for l in initial} == {('Prod', True), ('Non-Prod', True)}
    before = client.get('/v1/rbac/groups', headers=as_('admin')).json()
    volumes = []
    for name in ('One', 'Two'):
        response = client.post('/v1/workspaces', headers=as_('bob'), json={'name': name, 'editor': 'vscode', 'layer_id': layers.NON_PROD})
        assert response.status_code == 201, response.text
        ws = response.json()
        assert ws['layer_id'] == layers.NON_PROD and ws['layer_name'] == 'Non-Prod'
        assert client.post(f"/v1/workspaces/{ws['id']}/start", headers=as_('bob')).status_code == 200
        volumes.append(deps.get_orchestrator().executions['ws-'+ws['id']].home_volume)
    after = client.get('/v1/rbac/groups', headers=as_('admin')).json()
    assert after == before and len(set(volumes)) == 2
    assert client.post('/v1/workspaces', headers=as_('admin'), json={'name': 'Prod', 'editor': 'vscode', 'layer_id': layers.PROD}).status_code == 201
    assert client.post('/v1/workspaces', headers=as_('bob'), json={'name': 'Invalid', 'editor': 'vscode', 'layer_id': 'missing'}).status_code == 422


def test_layer_configuration_is_interactive_admin_only_and_does_not_grant_object_access(client):
    body = {'name': 'Sandbox', 'description': 'A third independent boundary', 'workspaces': True}
    assert client.post('/v1/admin/layers', headers=as_('ana'), json=body).status_code == 403
    result = client.post('/v1/admin/layers', headers=as_('admin'), json=body)
    assert result.status_code == 201, result.text
    layer = result.json()
    assert client.post('/v1/admin/layers', headers=as_('admin'), json={**body, 'name': 'sandbox'}).status_code == 409
    ws = client.post('/v1/workspaces', headers=as_('admin'), json={'name': 'Sandbox', 'editor': 'vscode', 'layer_id': layer['id']}).json()
    assert client.post('/v1/workspaces/'+ws['id']+'/start', headers=as_('admin')).status_code == 200
    token = {'Authorization': 'Bearer '+mint_token('execution', 'admin', 60, ws=ws['id'])}
    assert client.post('/v1/admin/layers', headers=token, json={**body, 'name': 'Escape'}).status_code == 403
    assert [l['id'] for l in client.get('/v1/layers', headers=token).json()] == [layer['id']]
    assert client.delete('/v1/admin/layers/'+layer['id'], headers=as_('admin')).status_code == 409
    assert client.put('/v1/admin/layers/'+layer['id'], headers=as_('admin'), json={**body, 'workspaces': False}).status_code == 200
    assert client.post('/v1/workspaces/'+ws['id']+'/stop', headers=as_('admin')).status_code == 200
    assert client.post('/v1/workspaces/'+ws['id']+'/start', headers=as_('admin')).status_code == 403
    spare = client.post('/v1/admin/layers', headers=as_('admin'), json={**body, 'name': 'Unused'}).json()
    assert client.delete('/v1/admin/layers/'+spare['id'], headers=as_('admin')).status_code == 204


@pytest.mark.parametrize('workspace_layer,allowed_catalog,denied_source', [
    (layers.NON_PROD, 'finance_test', 'finance-prod'),
    (layers.PROD, 'finance_prod', 'finance-test'),
])
def test_workspace_access_is_layer_intersected_with_personal_rbac_across_projects(client, workspace_layer, allowed_catalog, denied_source):
    from .test_api import onboard_workspace_group
    onboard_workspace_group(client,'ana')
    created = client.post('/v1/workspaces', headers=as_('ana'), json={'name': 'Workspace', 'editor': 'vscode', 'layer_id': workspace_layer})
    assert created.status_code == 201, created.text
    ws = created.json()
    assert ws['layer_id'] == workspace_layer
    client.get('/v1/me', headers=as_('ana'))
    started = client.post('/v1/workspaces/'+ws['id']+'/start', headers=as_('ana'))
    assert started.status_code == 200 and started.json()['status'] == 'running'
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
        for name, layer_id, grant in [('finance-test', layers.NON_PROD, True), ('other-test', layers.NON_PROD, False), ('finance-prod', layers.PROD, True)]:
            ds = DataSource(name=name, kind='postgres', config={'catalog': name.replace('-', '_'), 'layer_id': layer_id})
            db.add(ds); db.flush()
            source = rbac.source_resource(db, ds)
            db.add(Resource(kind='schema', name='analytics', parent_id=source.id, object_id=ds.id, config={'state':'ready'}))
            db.flush()
            if grant:
                rbac.own(db, ana, source)
        db.commit()
    token = 'Bearer '+mint_token('execution', 'ana', 60, ws=ws['id'])
    with SessionLocal() as db:
        scoped = sql_access.actor(db, token)
        human = rbac.resolve_user(db, User('ana'))
        assert scoped.user.environment_id == workspace_layer
        assert set(sql_access.scopes(db, scoped)) == {allowed_catalog}
        assert set(sql_access.scopes(db, sql_access.SqlActor(human, "human"))) == {'finance_test', 'finance_prod'}
    # Both Prod and Non-Prod workspaces exclude the other layer and ungranted projects.
    visible = client.get('/v1/rbac/resources', headers={'Authorization': token}).json()
    assert not any(r['name'] in (denied_source, 'other-test') for r in visible)
    assert any(r['name'] == allowed_catalog.replace('_', '-') for r in visible)


def test_data_layers_are_immutable_and_catalog_supports_layer_filters(client):
    result = client.put('/v1/admin/datasources/test-db', headers=as_('admin'), json=integration('test-db', 'postgres', layer_id=layers.NON_PROD))
    assert result.status_code == 200, result.text
    assert client.put('/v1/admin/datasources/test-db', headers=as_('admin'), json=integration('test-db', 'postgres', layer_id=layers.PROD)).status_code == 409
    source = next(r for r in client.get('/v1/rbac/resources', headers=as_('admin')).json() if r['name']=='test-db')
    schema = client.post('/v1/rbac/sources/test-db/schemas', headers=as_('admin'), json={'name':'analytics'}).json()
    assert schema['layer_id'] == layers.NON_PROD and source['layer_name'] == schema['layer_name'] == 'Non-Prod'
    result = client.get('/v1/catalog/search', headers=as_('admin'), params={'q':'layer:"Non-Prod"', 'layer': layers.NON_PROD}).json()
    assert result['total'] >= 2 and all(a['layer_id']==layers.NON_PROD for a in result['items'])
    assert result['facets']['layer'] == ['Non-Prod']
    assert client.get('/v1/catalog/search?layer=Prod', headers=as_('admin')).json()['total'] == 0
    client.get('/v1/me', headers=as_('ana'))
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
        pid = ana.principal_id
    # No intermediate workspace/security group is needed for a personal development schema.
    result = client.put(f"/v1/rbac/resources/{schema['id']}/assignments", headers=as_('admin'), json={'principal_id':pid,'role':'Admin'})
    assert result.status_code == 200 and any(a.get('principal_kind')=='user' and a['principal_id']==pid for a in result.json())
    # Direct grants still authorize access, but discovery requires an active app-group grant.
    assert not any(r['id']==schema['id'] for r in client.get('/v1/rbac/resources', headers=as_('ana')).json())
    with SessionLocal() as db:
        assert rbac.allowed(db,rbac.resolve_user(db,User('ana')),'ddl',db.get(Resource,schema['id']))


def test_initial_migration_preserves_private_permissions_files_and_layer_mapping(client):
    client.get('/v1/me', headers=as_('ana'))
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
        test = Resource(kind='folder', name='Test', parent_id='workloads', config={'environment':True, 'branch':'test', 'workspaces':True})
        prod = Resource(kind='folder', name='Prod', parent_id='workloads', config={'environment':True, 'branch':'main'})
        db.add_all([test, prod]); db.flush()
        ds = DataSource(name='legacy', kind='postgres', config={'environment_id':test.id})
        db.add(ds); db.flush()
        source = Resource(kind='source', name=ds.name, object_id=ds.id, config={'environment_id':test.id})
        db.add(source); db.flush()
        schema = Resource(kind='schema', name='dbt_ana', parent_id=source.id, object_id=ds.id)
        db.add(schema); db.flush()
        group = AppGroup(name='Pokemon Test Workspace Ana', owners=[ana.principal_id])
        db.add(group); db.flush()
        db.add(IdentityBinding(app_group_id=group.id, principal_id=ana.principal_id))
        db.add(RoleAssignment(resource_id=schema.id, app_group_id=group.id, role='Admin'))
        ws = Workspace(name='Legacy', owner='ana', principal_id=ana.principal_id, environment_id=test.id, volume_owner='keep-existing-volume', editor='vscode')
        db.add(ws); db.flush()
        db.execute(text("DELETE FROM fdp_migrations WHERE revision='002_layers'"))
        layers.migrate(db)
        db.commit()
        assert test.config['layer_id'] == layers.NON_PROD and prod.config['layer_id'] == layers.PROD
        assert ws.environment_id == layers.NON_PROD and ws.volume_owner == 'keep-existing-volume'
        assert not db.scalar(select(AppGroup).where(AppGroup.name=='Pokemon Test Workspace Ana'))
        assert rbac.data_scope(db, ana, ds) == {'dbt_ana':'Admin'}
        assert rbac.data_scope(db, rbac.resolve_user(db, User('bob')), ds) == {}
        direct = list(db.scalars(select(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id==schema.id)))
        assert len(direct)==1 and direct[0].principal_id==ana.principal_id
        layers.migrate(db)
        assert len(list(db.scalars(select(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id==schema.id)))) == 1


def test_moving_an_inherited_folder_keeps_its_layer_and_rejects_cross_layer_moves(client):
    def folder(name, parent='workloads', layer=None):
        response = client.post('/v1/rbac/folders', headers=as_('admin'), json={
            'name':name, 'parent_id':parent, **({'layer_id':layer} if layer else {})})
        assert response.status_code == 201, response.text
        return response.json()
    parent = folder('Development', layer=layers.NON_PROD)
    child = folder('Finance', parent['id'])
    nested = folder('Reports', child['id'])
    prod = folder('Production', layer=layers.PROD)
    result = client.patch('/v1/rbac/resources/'+child['id'], headers=as_('admin'),
        json={'name':'Finance', 'parent_id':'workloads'})
    assert result.status_code == 200 and result.json()['layer_id'] == layers.NON_PROD
    resources = client.get('/v1/rbac/resources', headers=as_('admin')).json()
    assert next(r for r in resources if r['id']==nested['id'])['layer_id'] == layers.NON_PROD
    result = client.patch('/v1/rbac/resources/'+child['id'], headers=as_('admin'),
        json={'name':'Finance', 'parent_id':prod['id']})
    assert result.status_code == 409
