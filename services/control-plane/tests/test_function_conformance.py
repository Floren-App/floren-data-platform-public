"""Independent function grants at real HTTP boundaries, without broad legacy roles."""
import pytest
from app import deps,rbac,secret_access
from app.auth import User
from app.db import SessionLocal
from app.identity import authorize_secrets,SecretAccessError
from .conftest import as_
from .rbac_helpers import integration


def grant(client,namespace,target,actions=None,role=None,layer=None,user='bob'):
    pid=client.get('/v1/me',headers=as_(user)).json()['principal_id']
    response=client.post('/v1/permissions/assignments',headers=as_('admin'),json={
        'target_id':target,'namespace':namespace,'layer_id':layer,'principal_id':pid,
        **({'role':role} if role else {'actions':actions})})
    assert response.status_code==201,response.text
    return response.json()


def test_personal_secret_viewer_does_not_write_delete_or_inject_values(client):
    assert client.put('/v1/secrets/user/me/private',headers=as_('bob'),json={'value':'private-value'}).status_code==403
    writer=grant(client,'secrets','secret:user:bob',role='Contributor')
    response=client.put('/v1/secrets/user/me/private',headers=as_('bob'),json={'value':'private-value'})
    assert response.status_code==200,response.text
    with SessionLocal() as db:
        with pytest.raises(SecretAccessError):
            authorize_secrets(db,deps.get_secret_store(),rbac.resolve_user(db,User('bob')),{'KEY':'me/private'})
    client.delete('/v1/permissions/assignments/'+writer['id'],headers=as_('admin')).raise_for_status()
    grant(client,'secrets','secret:user:bob',role='Viewer')
    rows=client.get('/v1/secrets',headers=as_('bob')).json()
    assert len(rows)==1 and rows[0]['capabilities']==[] and 'private-value' not in str(rows)
    assert client.put('/v1/secrets/user/me/private',headers=as_('bob'),json={'value':'overwrite'}).status_code==403
    assert client.delete('/v1/secrets/user/me/private',headers=as_('bob')).status_code==403


def test_storage_editor_cannot_rebind_destination_credentials_network_or_delete(client):
    body=integration('fn-files','s3')
    created=client.put('/v1/admin/datasources/fn-files',headers=as_('admin'),json=body)
    assert created.status_code==200,created.text
    source=created.json(); target=source['resource_id'];layer=source['config']['layer_id']
    grant(client,'storage.connection',target,['storage.connection.read','storage.connection.edit'],layer=layer)
    assert client.get('/v1/admin/datasources/fn-files',headers=as_('bob')).status_code==200
    body={'name':source['name'],'kind':source['kind'],'config':source['config'],'egress':source['egress'],'description':'Reviewed description'}
    edited=client.put('/v1/admin/datasources/fn-files',headers=as_('bob'),json=body)
    assert edited.status_code==200,edited.text
    assert 'storage.connection.credentials.change' not in edited.json()['capabilities']
    for patch in ({'config':{**body['config'],'bucket':'other-bucket'}},
                  {'provisioner_secret':{'access_key_id':'changed','secret_access_key':'changed'}},
                  {'egress':['10.1.0.0/16']}):
        assert client.put('/v1/admin/datasources/fn-files',headers=as_('bob'),json={**body,**patch}).status_code==403
    assert client.delete('/v1/admin/datasources/fn-files',headers=as_('bob')).status_code==403


def test_repository_editor_cannot_change_trust_credentials_or_remove(client):
    body={'name':'fn-repo','url':'https://example.com/repo.git','default_branch':'main','allowed_groups':[]}
    response=client.put('/v1/admin/repos/fn-repo',headers=as_('admin'),json=body)
    assert response.status_code==200,response.text
    target='repository:'+response.json()['id']
    grant(client,'repositories',target,['repositories.list','repositories.apply'])
    edited=client.put('/v1/admin/repos/fn-repo',headers=as_('bob'),json={**body,'description':'Reviewed'})
    assert edited.status_code==200,edited.text
    for patch in ({'url':'https://other.example.com/repo.git'}, {'secret':{'token':'changed'}}, {'allowed_groups':['other']}):
        assert client.put('/v1/admin/repos/fn-repo',headers=as_('bob'),json={**body,**patch}).status_code==403
    assert client.delete('/v1/admin/repos/fn-repo',headers=as_('bob')).status_code==403


def test_folder_rename_does_not_authorize_move_delete_or_permission_changes(client):
    folder=client.post('/v1/rbac/folders',headers=as_('admin'),json={'name':'Restricted folder','layer_id':'1a0000000002'}).json()
    destination=client.post('/v1/rbac/folders',headers=as_('admin'),json={'name':'Destination','layer_id':'1a0000000002'}).json()
    grant(client,'workloads.folders',folder['id'],['workloads.folders.rename'],layer='1a0000000002')
    response=client.patch('/v1/rbac/resources/'+folder['id'],headers=as_('bob'),json={'name':'Renamed','parent_id':'workloads'})
    assert response.status_code==200,response.text
    assert client.patch('/v1/rbac/resources/'+folder['id'],headers=as_('bob'),json={'name':'Moved','parent_id':destination['id']}).status_code==403
    assert client.delete('/v1/rbac/folders/'+folder['id'],headers=as_('bob')).status_code==403
    assert client.put('/v1/rbac/resources/'+folder['id']+'/assignments',headers=as_('bob'),json={'principal_id':client.get('/v1/me',headers=as_('bob')).json()['principal_id'],'role':'Owner'}).status_code==403


def test_query_results_need_their_function_in_addition_to_select():
    from types import SimpleNamespace
    from app.sql_access import query_function_allowed
    rules={'actions':['data.sql.select'],'tables':{}}
    query=SimpleNamespace(scope={'finance':{'schemas':{'public':'Named'},'named_objects':{'public':rules}}})
    from app.object_access import policy_allow
    assert not policy_allow(rules,'SelectFromColumns','cards',['price'])
    assert not query_function_allowed(query,'data.queries.results.read')
    rules['actions'].append('data.queries.results.read')
    assert query_function_allowed(query,'data.queries.results.read')
    assert policy_allow(rules,'SelectFromColumns','cards',['price'])
    sibling={'actions':['data.sql.select'],'tables':{}}
    assert not policy_allow(sibling,'SelectFromColumns','other',['price'])
    assert not query_function_allowed(query,'data.queries.cancel_own')


def test_directory_reader_cannot_edit_groups_or_inspect_ungranted_people(client):
    grant(client,'directory.people','people',['directory.people.list'])
    assert client.get('/v1/rbac/directory',headers=as_('bob')).json()==[]
    ana=client.get('/v1/me',headers=as_('ana')).json()['principal_id']
    grant(client,'directory.people','principal:'+ana,['directory.people.read'])
    rows=client.get('/v1/rbac/directory',headers=as_('bob')).json()
    assert [p['id'] for p in rows]==[ana]
    assert client.get('/v1/rbac/principals/'+ana+'/access',headers=as_('bob')).status_code==403


def test_every_registered_function_denies_an_ungranted_principal(client):
    from app import permissions
    pid=client.get('/v1/me',headers=as_('bob')).json()['principal_id']
    with SessionLocal() as db:
        actor=rbac.resolve_user(db,User('bob',principal_id=pid))
        for action in permissions.REGISTRY:
            assert not permissions.allowed(db,actor,action,'unassigned'),action
            assert not permissions.allowed(db,actor,action,'unassigned','1a0000000002'),action


def test_binding_inspection_grant_does_not_delegate_or_execute(client):
    folder=client.post('/v1/rbac/folders',headers=as_('admin'),json={'name':'Inspectable','layer_id':'1a0000000002'}).json()
    path='/v1/rbac/resources/'+folder['id']+'/assignments'
    assert client.get(path,headers=as_('bob')).status_code==403
    row=grant(client,'permissions',folder['id'],['permissions.bindings.read'],layer='1a0000000002')
    assert client.get(path,headers=as_('bob')).status_code==200
    pid=client.get('/v1/me',headers=as_('bob')).json()['principal_id']
    assert client.put(path,headers=as_('bob'),json={'principal_id':pid,'role':'Owner'}).status_code==403
    client.delete('/v1/permissions/assignments/'+row['id'],headers=as_('admin')).raise_for_status()
    assert client.get(path,headers=as_('bob')).status_code==403
