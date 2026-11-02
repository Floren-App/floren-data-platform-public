"""Exact capabilities, cross-layer isolation, delegation and live revocation."""
from app import permissions
from app.auth import User
from app.db import SessionLocal
from app.models import Resource
from .conftest import as_
from .test_deploying import repo  # noqa: F401


def grant(client, **changes):
    pid = client.get('/v1/me', headers=as_('bob')).json()['principal_id']
    body = dict(target_id='set-network', namespace='network.editor_allowlist', principal_id=pid,
                layer_id='1a0000000002', role='Manager')
    body.update(changes)
    result = client.post('/v1/permissions/assignments', headers=as_('admin'), json=body)
    assert result.status_code == 201, result.text
    return result.json()


def allowed(action, target='set-network', layer='1a0000000002'):
    with SessionLocal() as db:
        return permissions.allowed(db, User('bob'), action, target, layer)


def test_exact_actions_scope_and_revoke(client):
    assert not allowed('network.editor_allowlist.apply')
    row = grant(client)
    assert allowed('network.editor_allowlist.apply')
    assert not allowed('network.editor_allowlist.draft.edit')
    assert not allowed('network.editor_allowlist.apply', layer='1a0000000001')
    assert not allowed('network.editor_allowlist.ceiling.change')
    client.delete('/v1/permissions/assignments/' + row['id'], headers=as_('admin')).raise_for_status()
    assert not allowed('network.editor_allowlist.apply')


def test_global_and_explicit_all_layers(client):
    grant(client, layer_id=None)
    assert allowed('network.editor_allowlist.apply', layer=None)
    assert not allowed('network.editor_allowlist.apply')
    grant(client, layer_id='*')
    assert allowed('network.editor_allowlist.apply')


def test_all_layer_owner_does_not_own_global_ceiling(client):
    grant(client, layer_id='*', role='Owner')
    assert allowed('network.editor_allowlist.apply')
    assert not allowed('network.editor_allowlist.ceiling.change', layer=None)


def test_capability_lookup_does_not_modify_cached_group_membership(client):
    from .rbac_helpers import grant_group
    from app import rbac
    grant_group(client, 'workloads', 'First', 'Executor', users=['bob'])
    grant_group(client, 'workloads', 'Second', 'Executor', users=['bob'])
    with SessionLocal() as db:
        user = rbac.resolve_user(db, User('bob'))
        before = set(rbac.app_groups(db, user))
        assert len(before) == 2
        permissions.grants(db, user)
        assert rbac.app_groups(db, user) == before


def test_inherited_grants_do_not_cross_a_childs_actual_layer(client):
    with SessionLocal() as db:
        db.add(Resource(id='parent', kind='folder', name='Parent', config={'layer_id': '1a0000000002'}))
        db.flush()
        db.add(Resource(id='dev-child', kind='service', name='Dev', parent_id='parent', config={'layer_id': '1a0000000002'}))
        db.add(Resource(id='prod-child', kind='service', name='Prod', parent_id='parent', config={'layer_id': '1a0000000001'}))
        db.commit()
    grant(client, target_id='parent', namespace='workloads.code', role='Contributor')
    assert allowed('workloads.code.edit', 'dev-child')
    assert not allowed('workloads.code.edit', 'prod-child', '1a0000000001')


def test_no_selected_group_does_not_keep_a_hidden_admin_bypass(client):
    from app.models import UserPreference
    ident = client.get('/v1/me', headers=as_('admin')).json()['principal_id']
    with SessionLocal() as db:
        preference = db.get(UserPreference, ident) or UserPreference(principal_id=ident)
        preference.active_group_id = None
        db.add(preference)
        db.commit()
    result = client.get('/v1/me', headers=as_('admin')).json()
    assert not result['is_admin'] and not result['acting_admin']
    assert client.get('/v1/admin/audit', headers=as_('admin')).status_code == 403
    client.put('/v1/me/active-group', headers=as_('admin'), json={'group_id': '000000000001'}).raise_for_status()
    assert client.get('/v1/admin/audit', headers=as_('admin')).status_code == 200


def test_target_layer_cannot_be_spoofed(client):
    with SessionLocal() as db:
        db.add(Resource(id='test-policy', kind='network-policy', name='Test', config={'layer_id': '1a0000000001'}))
        db.commit()
    row = grant(client, target_id='test-policy', namespace='network.policies', layer_id='1a0000000001')
    assert allowed('network.policies.apply', 'test-policy', '1a0000000001')
    assert not allowed('network.policies.apply', 'test-policy', '1a0000000002')
    bad = client.put('/v1/permissions/assignments/' + row['id'], headers=as_('admin'), json={
        **{k: v for k, v in row.items() if k != 'id'}, 'layer_id': '1a0000000002'})
    assert bad.status_code == 422


def test_single_action_and_nonowner_cannot_delegate(client):
    row = grant(client, role=None, actions=['network.editor_allowlist.read'])
    assert allowed('network.editor_allowlist.read')
    assert not allowed('network.editor_allowlist.apply')
    assert client.post('/v1/permissions/assignments', headers=as_('bob'), json={
        **{k: v for k, v in row.items() if k != 'id'}, 'actions': ['network.editor_allowlist.apply']}).status_code == 403


def test_owner_scope_delegation(client):
    grant(client, role='Owner')
    ana = client.get('/v1/me', headers=as_('ana')).json()['principal_id']
    body = dict(target_id='set-network', namespace='network.editor_allowlist', principal_id=ana,
                layer_id='1a0000000002', actions=['network.editor_allowlist.read'])
    assert client.post('/v1/permissions/assignments', headers=as_('bob'), json=body).status_code == 201
    assert client.post('/v1/permissions/assignments', headers=as_('bob'), json={**body, 'layer_id': '*'}).status_code == 403
    assert client.post('/v1/permissions/assignments', headers=as_('bob'), json={**body, 'layer_id': '1a0000000001'}).status_code == 403


def test_custom_role_changes_live_and_cannot_delete_assigned(client):
    response = client.post('/v1/permissions/roles', headers=as_('admin'), json={
        'name': 'Inspector', 'actions': ['network.editor_allowlist.read']})
    assert response.status_code == 201, response.text
    role = response.json()
    row = grant(client, role=None, custom_role_id=role['id'])
    assert allowed('network.editor_allowlist.read')
    assert client.delete('/v1/permissions/roles/' + role['id'], headers=as_('admin')).status_code == 409
    client.put('/v1/permissions/roles/' + role['id'], headers=as_('admin'), json={
        'name': 'Inspector', 'actions': ['network.editor_allowlist.apply']}).raise_for_status()
    assert not allowed('network.editor_allowlist.read')
    assert allowed('network.editor_allowlist.apply')
    client.delete('/v1/permissions/assignments/' + row['id'], headers=as_('admin')).raise_for_status()
    client.delete('/v1/permissions/roles/' + role['id'], headers=as_('admin')).raise_for_status()


def test_disabled_recipient_can_be_revoked(client):
    from app.models import DirectoryPrincipal
    row = grant(client)
    with SessionLocal() as db:
        db.get(DirectoryPrincipal, row['principal_id']).active = False
        db.commit()
    assert client.delete('/v1/permissions/assignments/' + row['id'], headers=as_('admin')).status_code == 204


def page(client, target):
    return grant(client, target_id=target, namespace='pages', layer_id=None, role='Viewer')


def test_network_operator_is_confined_to_its_layer(client):
    endpoint = '/v1/admin/settings/egress'
    assert client.get(endpoint, headers=as_('bob')).status_code == 403
    page(client, 'set-network')
    grant(client)
    assert client.get(endpoint, headers=as_('bob'), params={'layer_id': '1a0000000002'}).status_code == 200
    assert client.get(endpoint, headers=as_('bob'), params={'layer_id': '1a0000000001'}).status_code == 403
    assert client.get(endpoint, headers=as_('bob')).status_code == 403
    client.put(endpoint, headers=as_('admin'), json={'hosts': [{'host': '*.example.com'}]}).raise_for_status()
    write = lambda host, layer='1a0000000002': client.put(endpoint, headers=as_('bob'), params={'layer_id': layer}, json={'hosts': [{'host': host}]})
    assert write('dev.example.com').status_code == 200
    assert write('other.example.net').status_code == 422
    assert write('dev.example.com', '1a0000000001').status_code == 403


def test_page_view_and_form_edit_publish_are_independent(client):
    body = {'name': 'Purpose', 'definition': {'pages': [{'id': 'p', 'title': 'P', 'fields': [
        {'id': 'why', 'label': 'Why', 'type': 'short_text'}]}]}}
    assert client.get('/v1/compliance/forms', headers=as_('bob')).status_code == 403
    assert client.get('/v1/compliance/policies', headers=as_('bob')).status_code == 403
    assert client.get('/v1/compliance/roles', headers=as_('bob')).status_code == 403
    grant(client, target_id='set-forms', namespace='compliance.forms', layer_id=None, role='Contributor')
    assert client.put('/v1/compliance/forms/Purpose', headers=as_('bob'), json=body).status_code == 403
    page(client, 'set-forms')
    response = client.put('/v1/compliance/forms/Purpose', headers=as_('bob'), json=body)
    assert response.status_code == 200, response.text
    assert client.post('/v1/compliance/forms/' + response.json()['id'] + '/publish', headers=as_('bob')).status_code == 403
    grant(client, target_id='set-forms', namespace='compliance.forms', layer_id=None, role=None, actions=['compliance.forms.publish'])
    assert client.post('/v1/compliance/forms/' + response.json()['id'] + '/publish', headers=as_('bob')).status_code == 200
    assert client.delete('/v1/compliance/forms/' + response.json()['id'], headers=as_('bob')).status_code == 403


def test_scoped_layer_creation_is_delegable(client):
    from .rbac_helpers import grant_group
    gid = grant_group(client, 'workloads', 'Layer builders', 'Executor', users=['bob'])
    client.put('/v1/me/active-group', headers=as_('bob'), json={'group_id': gid}).raise_for_status()
    page(client, 'set-layers')
    grant(client, target_id='set-layers', namespace='layers.collection', layer_id=None, role='Manager')
    response = client.post('/v1/admin/layers', headers=as_('bob'), json={'name': 'Sandbox'})
    assert response.status_code == 201, response.text
    assert client.post('/v1/admin/layers', headers=as_('bob'), json={'name': 'Protected', 'production': True}).status_code == 403


def test_proxy_uses_current_workspace_layer_and_rejects_stopped_identity(client, monkeypatch):
    from app.config import get_settings
    from app import deps
    monkeypatch.setattr(get_settings(), "egress_proxy_token", "proxy-service-proof")
    from app import egress
    from app.auth import mint_token
    from app.models import Workspace
    pid = client.get('/v1/me', headers=as_('bob')).json()['principal_id']
    with SessionLocal() as db:
        egress.set_hosts(db, [{'host': '*.example.com'}])
        egress.set_hosts(db, [{'host': 'dev.example.com', 'source_ip': '10.42.1.10'}], '1a0000000002')
        egress.set_hosts(db, [{'host': 'prod.example.com'}], '1a0000000001')
        ws = Workspace(owner='bob', name='Dev', editor='vscode', status='running', principal_id=pid, environment_id='1a0000000002', execution_ref='ws-dev')
        db.add(ws); db.flush(); ident = ws.id; db.commit()
    deps.get_orchestrator().addresses['ws-dev'] = ['10.42.1.10']
    headers = {'Authorization': 'Bearer ' + mint_token('egress', ident, 60), 'X-FDP-Egress-Proxy': 'proxy-service-proof'}
    endpoint = '/internal/egress/authorize'
    assert client.post(endpoint, json={'host': 'dev.example.com', 'source_ip': '10.42.1.10'}).status_code == 401
    assert client.post(endpoint, headers=headers, json={'host': 'dev.example.com', 'source_ip': '10.42.1.10'}).json()['allowed']
    assert not client.post(endpoint, headers=headers, json={'host': 'prod.example.com', 'source_ip': '10.42.1.10', 'layer_id': '1a0000000001'}).json()['allowed']
    assert client.post(endpoint, headers=headers, json={'host': 'dev.example.com', 'source_ip': '10.42.2.20'}).status_code == 403
    with SessionLocal() as db:
        db.get(Workspace, ident).status = 'stopped'; db.commit()
    assert client.post(endpoint, headers=headers, json={'host': 'dev.example.com', 'source_ip': '10.42.1.10'}).status_code == 403


def test_workload_auto_update_permission_cannot_change_identity_or_run(client, repo):
    from .test_deploying import deploy
    from .rbac_helpers import grant_group, select_group
    pub = deploy(client, repo, program='sales-report', slug='fine-workload').json()
    group = grant_group(client, pub['resource_id'], 'Workload viewers', 'Viewer', users=['bob'])
    select_group(client, 'bob', group)
    with SessionLocal() as db:
        from app.permissions import resource_layer
        layer = resource_layer(db,pub['resource_id'])
    pid = client.get('/v1/me',headers=as_('bob')).json()['principal_id']
    response = client.post('/v1/permissions/assignments',headers=as_('admin'),json={
        'target_id':pub['resource_id'],'namespace':'workloads.code','layer_id':layer,
        'principal_id':pid,'actions':['workloads.code.auto_update.set']})
    assert response.status_code == 201,response.text
    path = '/v1/workloads/'+pub['id']
    assert client.patch(path,headers=as_('bob'),json={'auto_update':True}).status_code == 200
    for changes in ({'title':'Unpermitted'}, {'run_as':''}, {'execution_mode':'fixed'}, {'secrets':{}}, {'env':{}}):
        assert client.patch(path,headers=as_('bob'),json=changes).status_code == 403
    assert client.post(path+'/sync',headers=as_('bob')).status_code == 403
    assert client.post(path+'/runs',headers=as_('bob'),json={}).status_code == 403
    assert client.delete(path,headers=as_('bob')).status_code == 403


def test_token_inventory_and_creation_do_not_grant_revocation_or_secret_writes(client):
    from .test_api import setup_finance_db
    from .test_platform import make_sa
    from .rbac_helpers import grant_group, select_group
    setup_finance_db(client);make_sa(client)
    resources=client.get('/v1/rbac/resources',headers=as_('admin')).json()
    target=next(r for r in resources if r['config'].get('service_account'))
    group=grant_group(client,target['id'],'Credential auditors','Viewer',users=['bob'])
    select_group(client,'bob',group)
    grant(client,target_id=target['id'],namespace='identities.tokens',layer_id=target['layer_id'],
        role=None,actions=['identities.tokens.list'])
    path='/v1/service-accounts/finance-etl/tokens'
    assert client.get(path,headers=as_('bob')).status_code==200
    assert client.post(path,headers=as_('bob'),json={'name':'denied'}).status_code==403
    assert client.delete(path+'/unknown',headers=as_('bob')).status_code==403
    grant(client,target_id=target['id'],namespace='identities.tokens',layer_id=target['layer_id'],
        role=None,actions=['identities.tokens.create'])
    created=client.post(path,headers=as_('bob'),json={'name':'automation'})
    assert created.status_code==201,created.text
    assert created.json()['token'].startswith('dcs_')
    assert client.get(path,headers=as_('bob')).json()[0]['token'] is None
    assert client.post(path,headers=as_('bob'),json={'name':'secret','store_as_secret':'unexpected'}).status_code==403
    assert client.delete(path+'/'+created.json()['id'],headers=as_('bob')).status_code==403


def test_workload_viewer_needs_a_separate_permission_for_log_contents(client,repo):
    from .test_deploying import deploy
    from .rbac_helpers import grant_group, select_group
    from app.models import Workload
    from app.workload_logs import entry
    pub=deploy(client,repo,program='sales-report',slug='log-boundary').json()
    group=grant_group(client,pub['resource_id'],'Metadata observers','Viewer',users=['bob'])
    select_group(client,'bob',group)
    with SessionLocal() as db:
        entry(db,workload=db.get(Workload,pub['id']),level='info',message='Private application payload')
        db.commit()
    path='/v1/logs?workload_id='+pub['id']
    assert client.get('/v1/workloads/'+pub['id'],headers=as_('bob')).status_code==200
    denied=client.get(path,headers=as_('bob'))
    assert denied.status_code==200 and denied.json()['entries']==[]
    grant(client,target_id=pub['resource_id'],namespace='logs',layer_id=pub['layer_id'],
        role=None,actions=['logs.read'])
    response=client.get(path,headers=as_('bob'))
    assert response.status_code==200,response.text
    assert 'Private application payload' in response.text


def test_api_metadata_edit_does_not_rebind_credentials_or_change_trust(client):
    from .rbac_helpers import grant_group, select_group
    client.put('/v1/secrets/group/platform/crm-token',headers=as_('admin'),json={'value':'private-value'}).raise_for_status()
    body={'name':'CRM','slug':'crm-boundary','direction':'external','outbound':{
        'base_url':'https://crm.example.com','auth':{'type':'bearer','secret':'group:platform/crm-token'}}}
    created=client.post('/v1/apis',headers=as_('admin'),json=body)
    assert created.status_code==201,created.text
    api=created.json()
    group=grant_group(client,api['resource_id'],'API metadata editors','Viewer',users=['bob'])
    select_group(client,'bob',group)
    with SessionLocal() as db:
        layer=permissions.resource_layer(db,api['resource_id'])
    grant(client,target_id=api['resource_id'],namespace='apis.object',layer_id=layer,
        role=None,actions=['apis.object.apply'])
    path='/v1/apis/'+api['id']
    edited=client.put(path,headers=as_('bob'),json={**body,'name':'Renamed CRM'})
    assert edited.status_code==200,edited.text
    assert 'private-value' not in edited.text
    for change in ({'base_url':'https://other.example.com'},{'verify_tls':False},
                   {'auth':{'type':'none'}}):
        assert client.put(path,headers=as_('bob'),json={**body,'outbound':{**body['outbound'],**change}}).status_code==403
    assert client.delete(path,headers=as_('bob')).status_code==403
