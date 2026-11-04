from types import SimpleNamespace

from sqlalchemy import select, text

from app import deps, layers, ownership, rbac, sql_access
from app.auth import User
from app.db import SessionLocal
from app.models import (AppGroup, CatalogAsset, DirectoryPrincipal, IdentityBinding, Resource,
                        RoleAssignment, ServiceRoleAssignment, UserPreference)
from .conftest import as_
from .rbac_helpers import grant_group, integration
from .test_deploying import deploy
from .test_deploying import repo  # noqa: F401


def groups(client):
    parent = client.post('/v1/rbac/folders',headers=as_('admin'),json={'name':'Project','layer_id':layers.NON_PROD}).json()
    alpha = grant_group(client,parent['id'],'Alpha creators','Admin',users=['ana'])
    beta = grant_group(client,parent['id'],'Beta readers','Executor',users=['ana'])
    return parent,alpha,beta


def select_group(client,user,group):
    response = client.put('/v1/me/active-group',headers=as_(user),json={'group_id':group})
    assert response.status_code == 200,response.text
    return response


def test_primary_group_owns_new_objects_and_secondary_groups_cannot_authorize_creation(client):
    parent,alpha,beta = groups(client)
    select_group(client,'ana',alpha)
    child = client.post('/v1/rbac/folders',headers=as_('ana'),json={'name':'Created','parent_id':parent['id']})
    assert child.status_code == 201,child.text
    assert child.json()['owner_group_id'] == alpha and child.json()['owner_group_name']=='Alpha creators'
    assignments = client.get('/v1/rbac/resources/'+child.json()['id']+'/assignments',headers=as_('ana')).json()
    assert any(a['app_group_id']==alpha and a['role']=='Owner' and not a['inherited'] for a in assignments)
    select_group(client,'ana',beta)
    assert client.get('/v1/me',headers=as_('ana')).json()['active_group_id']==beta
    # Inactive group membership cannot authorize mutation or delegation.
    rows = client.get('/v1/rbac/resources',headers=as_('ana')).json()
    assert 'assign' not in next(r for r in rows if r['id']==child.json()['id'])['actions']
    denied = client.post('/v1/rbac/folders',headers=as_('ana'),json={'name':'Denied','parent_id':parent['id']})
    assert denied.status_code==403
    assert not any(g['name'].startswith('Personal owners ') for g in client.get('/v1/rbac/groups',headers=as_('admin')).json())


def test_browsing_and_direct_access_follow_active_group_with_inheritance(client,repo):
    folders=[];groups=[];pubs=[];schemas=[]
    for suffix,layer in (('alpha',layers.NON_PROD),('beta',layers.PROD)):
        folder=client.post('/v1/rbac/folders',headers=as_('admin'),json={'name':suffix,'layer_id':layer}).json()
        group=grant_group(client,folder['id'],suffix+' team','Admin',users=['ana','admin'])
        result=deploy(client,repo,user='admin',program='region-summary',slug=suffix,folder_id=folder['id'],layer_id=layer)
        assert result.status_code==201,result.text
        client.put('/v1/admin/datasources/'+suffix,headers=as_('admin'),json=integration(suffix,'postgres',layer_id=layer)).raise_for_status()
        schema=client.post('/v1/rbac/sources/'+suffix+'/schemas',headers=as_('admin'),json={'name':'analytics'}).json()
        client.put('/v1/rbac/resources/'+schema['id']+'/assignments',headers=as_('admin'),json={'app_group_id':group,'role':'DataReader'}).raise_for_status()
        with SessionLocal() as db:
            from app import catalog
            catalog.index_children(db,db.get(Resource,schema['id']),[{'kind':'table','name':suffix+'_cards'}])
            db.commit()
        folders.append(folder);groups.append(group);pubs.append(result.json());schemas.append(schema)
    for username in ('ana','admin'):
        for index in (0,1,0):
            select_group(client,username,groups[index])
            listing=client.get('/v1/workloads',headers=as_(username)).json()
            assert {p['id'] for p in listing}=={pubs[index]['id']}
            resources=client.get('/v1/rbac/resources',headers=as_(username)).json()
            ids={r['id'] for r in resources}
            assert schemas[index]['id'] in ids and schemas[1-index]['id'] not in ids
            assert folders[index]['id'] in ids and folders[1-index]['id'] not in ids
            assert 'workloads' in ids  # Name-only ancestors still provide navigation.
            me=client.get('/v1/me',headers=as_(username)).json()
            assert {d['name'] for d in me['data_sources']}=={('alpha','beta')[index]}
            found=client.get('/v1/catalog/search',headers=as_(username),params={'q':'cards'}).json()
            assert {a['name'] for a in found['items']}=={('alpha','beta')[index]+'_cards'}
            suggestions=client.get('/v1/catalog/suggest',headers=as_(username),params={'q':'cards'}).json()
            assert ('alpha','beta')[1-index]+'_cards' not in str(suggestions)
            # Direct URLs cannot borrow authority from an inactive group.
            assert client.get('/v1/workloads/'+pubs[1-index]['id'],headers=as_(username)).status_code in (403,404)
    select_group(client,'admin',ownership.GLOBAL_ADMIN)
    assert {p['id'] for p in client.get('/v1/workloads',headers=as_('admin')).json()}=={p['id'] for p in pubs}
    # Revoking selected-group membership never falls back to another group's listings.
    select_group(client,'ana',groups[0])
    ana=client.get('/v1/me',headers=as_('ana')).json()
    client.delete('/v1/rbac/groups/'+groups[0]+'/bindings/'+ana['principal_id'],headers=as_('admin')).raise_for_status()
    assert client.get('/v1/workloads',headers=as_('ana')).json()==[]
    assert client.get('/v1/catalog/search',headers=as_('ana')).json()['items']==[]


def test_workload_owner_is_primary_group_and_secondary_create_is_rejected(client,repo):
    parent,alpha,beta = groups(client)
    select_group(client,'ana',beta)
    assert deploy(client,repo,program='region-summary',slug='denied',folder_id=parent['id']).status_code==403
    select_group(client,'ana',alpha)
    result = deploy(client,repo,program='region-summary',slug='allowed',folder_id=parent['id'])
    assert result.status_code==201,result.text
    resource = next(r for r in client.get('/v1/rbac/resources',headers=as_('ana')).json() if r['id']==result.json()['resource_id'])
    assert resource['owner_group_id']==alpha


def test_membership_is_live_and_group_selection_is_not_an_escalation(client):
    parent,alpha,beta = groups(client)
    select_group(client,'ana',alpha)
    assert client.put('/v1/me/active-group',headers=as_('ana'),json={'group_id':ownership.GLOBAL_ADMIN}).status_code==403
    with SessionLocal() as db:
        ana = rbac.resolve_user(db,User('ana'))
        pid = ana.principal_id
    assert client.delete(f'/v1/rbac/groups/{alpha}/bindings/{pid}',headers=as_('admin')).status_code==204
    me = client.get('/v1/me',headers=as_('ana')).json()
    assert me['active_group_id']==alpha and alpha not in {g['id'] for g in me['available_groups']}
    assert client.post('/v1/rbac/folders',headers=as_('ana'),json={'name':'Denied','parent_id':parent['id']}).status_code==403
    select_group(client,'ana',beta)
    # Execution credentials cannot change an interactive user's primary group.
    from app import auth
    from app.main import app
    previous=app.dependency_overrides[auth.get_current_user]
    app.dependency_overrides[auth.get_current_user]=lambda: User('ana',principal_id=pid,
        environment_id=layers.NON_PROD,execution_context='workspace:missing')
    try:
        assert client.put('/v1/me/active-group',headers=as_('ana'),json={'group_id':beta}).status_code==403
    finally:
        app.dependency_overrides[auth.get_current_user]=previous


def test_creation_captures_primary_group_for_the_entire_request(client):
    parent,alpha,beta = groups(client)
    select_group(client,'ana',alpha)
    with SessionLocal() as db:
        admitted = rbac.resolve_user(db,User('ana'))
        preference = db.get(UserPreference,admitted.principal_id)
        preference.active_group_id=beta
        db.commit()
        assert rbac.resolve_user(db,User('ana')).active_group_id==beta
        resource = db.get(Resource,parent['id'])
        assert ownership.require_create(db,admitted,resource).id==alpha
        child = Resource(kind='folder',name='Same request',parent_id=resource.id)
        db.add(child);db.flush()
        rbac.own(db,admitted,child)
        assert child.config['owner_group_id']==alpha


def test_global_admin_is_protected_live_and_has_owner_access_to_future_objects(client):
    parent,alpha,_ = groups(client)
    select_group(client,'ana',alpha)
    child = client.post('/v1/rbac/folders',headers=as_('ana'),json={'name':'Future','parent_id':parent['id']}).json()
    bob = client.get('/v1/me',headers=as_('bob')).json()
    gid = ownership.GLOBAL_ADMIN
    assert client.post(f'/v1/rbac/groups/{gid}/bindings',headers=as_('admin'),json={'principal_id':bob['principal_id']}).status_code==201
    assert client.get('/v1/me',headers=as_('bob')).json()['is_admin']
    rows = client.get('/v1/rbac/resources',headers=as_('bob')).json()
    assert 'assign' in next(r for r in rows if r['id']==child['id'])['actions']
    assert client.delete(f'/v1/rbac/groups/{gid}',headers=as_('admin')).status_code==409
    assert client.put(f'/v1/rbac/groups/{gid}',headers=as_('admin'),json={'name':'Changed','owners':[bob['principal_id']]}).status_code==409
    assert client.delete(f'/v1/rbac/resources/{child["id"]}/assignments/{gid}',headers=as_('bob')).status_code==409
    grant = next(a for a in client.get(f'/v1/rbac/resources/{child["id"]}/assignments',headers=as_('ana')).json() if a.get('system'))
    assert grant['role']=='Owner' and grant['app_group_id']==gid
    assert client.delete(f'/v1/rbac/groups/{gid}/bindings/{bob["principal_id"]}',headers=as_('admin')).status_code==204
    assert not client.get('/v1/me',headers=as_('bob')).json()['is_admin']


def test_sql_uses_selected_group_and_services_keep_direct_grants(client):
    _,alpha,beta = groups(client)
    client.put('/v1/admin/datasources/primary-db',headers=as_('admin'),json=integration('primary-db','postgres')).raise_for_status()
    schema = client.post('/v1/rbac/sources/primary-db/schemas',headers=as_('admin'),json={'name':'analytics'}).json()
    for group,role in ((alpha,'Admin'),(beta,'DataReader')):
        client.put(f'/v1/rbac/resources/{schema["id"]}/assignments',headers=as_('admin'),json={'app_group_id':group,'role':role}).raise_for_status()
    with SessionLocal() as db:
        db.get(Resource,schema['id']).config={'state':'ready'}
        db.commit()
    select_group(client,'ana',beta)
    with SessionLocal() as db:
        user=rbac.resolve_user(db,User('ana'))
        scope=sql_access.scopes(db,sql_access.SqlActor(user,'human'))
        query=SimpleNamespace(scope=scope,principal_id=user.principal_id)
        table={'table':{'catalogName':'primary_db','schemaName':'analytics','tableName':'cards'}}
        assert scope['primary_db']['schemas']['analytics']=='Reader'
        assert not sql_access.policy_allow(query,{'operation':'CreateTable','resource':table})
        assert not sql_access.policy_allow(query,{'operation':'DropTable','resource':table})
        assert sql_access.policy_allow(query,{'operation':'SelectFromColumns','resource':table})
    select_group(client,'ana',alpha)
    with SessionLocal() as db:
        scope=sql_access.scopes(db,sql_access.SqlActor(rbac.resolve_user(db,User('ana')),'human'))
        assert sql_access.policy_allow(SimpleNamespace(scope=scope),{'operation':'CreateTable','resource':table})
        service=rbac.principal(db,'service','ee0000000001','Direct service','local')
        db.add(Resource(kind='service',name='Direct service',object_id=service.external_id,config={'layer_id':layers.NON_PROD}))
        db.add(ServiceRoleAssignment(resource_id=schema['id'],principal_id=service.id,role='Admin'))
        db.commit()
        user=rbac.resolve_user(db,User('service:'+service.id))
        scope=sql_access.scopes(db,sql_access.SqlActor(user,'service'))
        assert sql_access.policy_allow(SimpleNamespace(scope=scope),{'operation':'CreateTable','resource':table})


def test_migration_removes_personal_groups_and_preserves_exact_nonadmin_access(client):
    client.get('/v1/me',headers=as_('ana'))
    with SessionLocal() as db:
        ana=rbac.resolve_user(db,User('ana'))
        group=AppGroup(name='Personal owners '+ana.principal_id,owners=[ana.principal_id])
        db.add(group);db.flush()
        db.add(IdentityBinding(app_group_id=group.id,principal_id=ana.principal_id))
        resource=Resource(kind='folder',name='Private legacy',parent_id='workloads')
        db.add(resource);db.flush()
        db.add(RoleAssignment(resource_id=resource.id,app_group_id=group.id,role='Owner'))
        asset=CatalogAsset(id='f'*32,resource_id=resource.id,name=resource.name,kind='folder',path='Private legacy',governance={'owner_group_id':group.id})
        db.add(asset);db.flush()
        gid=group.id
        db.execute(text("DELETE FROM fdp_migrations WHERE revision='003_active_groups'"))
        ownership.migrate(db);db.commit()
        assert db.get(AppGroup,gid) is None
        assert rbac.allowed(db,ana,'assign',resource)
        assert not rbac.allowed(db,rbac.resolve_user(db,User('bob')),'read',resource)
        assert asset.governance['owner_group_id'] is None
        assert db.scalar(select(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id==resource.id)).principal_id==ana.principal_id


def test_workspaces_use_existing_primary_group_without_sharing_personal_execution(client):
    _,alpha,_ = groups(client)
    from tests.rbac_helpers import workspace_permissions
    workspace_permissions(client,alpha)
    select_group(client,'ana',alpha)
    before=client.get('/v1/rbac/groups',headers=as_('admin')).json()
    ws=client.post('/v1/workspaces',headers=as_('ana'),json={'name':'Private','editor':'vscode'}).json()
    assert ws['owner_group_id']==alpha and ws['owner_group_name']=='Alpha creators'
    assert client.get('/v1/rbac/groups',headers=as_('admin')).json()==before
    bob=client.get('/v1/me',headers=as_('bob')).json()
    client.post(f'/v1/rbac/groups/{alpha}/bindings',headers=as_('admin'),json={'principal_id':bob['principal_id']}).raise_for_status()
    assert client.get('/v1/workspaces/'+ws['id']+'/config',headers=as_('bob')).status_code==404
    assert client.get('/v1/workspaces/'+ws['id']+'/config',headers=as_('admin')).status_code==200


def test_job_keeps_admitted_primary_creation_rights_after_switch_and_revocation(client):
    _,alpha,beta=groups(client)
    from tests.rbac_helpers import workspace_permissions
    workspace_permissions(client,alpha)
    client.put('/v1/admin/datasources/snap-db',headers=as_('admin'),json=integration('snap-db','postgres')).raise_for_status()
    schema=client.post('/v1/rbac/sources/snap-db/schemas',headers=as_('admin'),json={'name':'analytics'}).json()
    for group,role in ((alpha,'Admin'),(beta,'DataReader')):
        client.put(f'/v1/rbac/resources/{schema["id"]}/assignments',headers=as_('admin'),json={'app_group_id':group,'role':role}).raise_for_status()
    with SessionLocal() as db:
        db.get(Resource,schema['id']).config={'state':'ready'};db.commit()
    select_group(client,'ana',alpha)
    ws=client.post('/v1/workspaces',headers=as_('ana'),json={'name':'Snapshot','editor':'vscode'}).json()
    admitted=client.post('/v1/runs',headers=as_('ana'),json={'workspace_id':ws['id'],'kind':'script','entrypoint':'x.py','pool':'small'})
    assert admitted.status_code==201,admitted.text
    run=admitted.json()
    token=deps.get_orchestrator().executions['run-'+run['id']].env['FDP_SESSION_TOKEN']
    select_group(client,'ana',beta)
    client.delete(f'/v1/rbac/resources/{schema["id"]}/assignments/{alpha}',headers=as_('admin')).raise_for_status()
    with SessionLocal() as db:
        who=sql_access.actor(db,'Bearer '+token)
        assert who.user.active_group_id==alpha
        old=sql_access.scopes(db,who)
        fresh=sql_access.scopes(db,sql_access.SqlActor(rbac.resolve_user(db,User('ana')),'human'))
        assert old['snap_db']['schemas']['analytics']=='Admin' and old['snap_db']['create_schemas']==['analytics']
        assert fresh['snap_db']['schemas']['analytics']=='Reader' and not fresh['snap_db']['create_schemas']


def test_workspace_functions_are_explicit_layer_scoped_and_private(client):
    from .rbac_helpers import workspace_permissions
    _,alpha,_=groups(client)
    select_group(client,'ana',alpha)
    body={'name':'Private dev','editor':'sql','layer_id':layers.NON_PROD}
    assert client.post('/v1/workspaces',headers=as_('ana'),json=body).status_code==403
    def grant(role,target='workspaces',layer=layers.NON_PROD):
        response=client.post('/v1/permissions/assignments',headers=as_('admin'),json={
            'namespace':'workspaces','target_id':target,'layer_id':layer,'app_group_id':alpha,'role':role})
        assert response.status_code==201,response.text
        return response.json()['id']
    grant('Contributor')
    response=client.post('/v1/workspaces',headers=as_('ana'),json=body)
    assert response.status_code==201,response.text
    ws=response.json(); path='/v1/workspaces/'+ws['id']
    assert 'workspaces.object.start' not in ws['capabilities']
    spoof=client.post('/v1/permissions/assignments',headers=as_('admin'),json={
        'namespace':'workspaces','target_id':'workspace:'+ws['id'],'layer_id':layers.PROD,
        'app_group_id':alpha,'role':'Admin'})
    assert spoof.status_code==422
    assert client.post(path+'/start',headers=as_('ana')).status_code==403
    assert client.post('/v1/runs',headers=as_('ana'),json={'workspace_id':ws['id'],'kind':'script','entrypoint':'x.py','pool':'small'}).status_code==403
    assert client.get(path+'/config',headers=as_('ana')).status_code==403
    assert client.post('/v1/workspaces',headers=as_('ana'),json={**body,'layer_id':layers.PROD}).status_code==403
    grant('Executor',target='workspace:'+ws['id'])
    assert client.post(path+'/start',headers=as_('ana')).status_code==200
    assert client.post(path+'/open',headers=as_('ana')).status_code==403
    grant('DataContributor',target='workspace:'+ws['id'])
    assert client.post(path+'/open',headers=as_('ana')).status_code==200
    assert client.delete(path,headers=as_('ana')).status_code==403
    bob=client.get('/v1/me',headers=as_('bob')).json()
    client.post('/v1/rbac/groups/'+alpha+'/bindings',headers=as_('admin'),json={'principal_id':bob['principal_id']}).raise_for_status()
    select_group(client,'bob',alpha)
    assert client.post(path+'/open',headers=as_('bob')).status_code==404
    assert client.get('/v1/workspaces',headers=as_('bob')).json()==[]


def test_queued_workspace_start_rechecks_revoked_permission(client):
    from .rbac_helpers import workspace_permissions
    from app.models import Workspace, PermissionGrant
    from app.routes.workspaces import launch_workspace
    _,alpha,_=groups(client)
    select_group(client,'ana',alpha)
    workspace_permissions(client,alpha)
    response=client.post('/v1/workspaces',headers=as_('ana'),json={'name':'Queued','editor':'sql'})
    assert response.status_code==201,response.text
    with SessionLocal() as db:
        ws=db.get(Workspace,response.json()['id'])
        ws.status='queued'
        for assignment in db.scalars(select(PermissionGrant).where(PermissionGrant.app_group_id==alpha)):
            db.delete(assignment)
        db.commit()
        orch=deps.get_orchestrator()
        before=set(orch.executions)
        launch_workspace(db,orch,ws)
        assert ws.status=='failed' and 'revoked' in ws.startup_error
        assert set(orch.executions)==before


def test_run_owner_needs_separate_logs_artifacts_and_cancel_functions(client):
    from .test_api import start_workspace
    from app.models import PermissionGrant
    ws=start_workspace(client)
    response=client.post('/v1/runs',headers=as_('ana'),json={
        'workspace_id':ws,'kind':'app','entrypoint':'app.py','pool':'small'})
    assert response.status_code==201,response.text
    run=response.json();path='/v1/runs/'+run['id']
    # Retain workspace and run lifecycle grants; revoke only payload grants.
    with SessionLocal() as db:
        for assignment in db.scalars(select(PermissionGrant).where(PermissionGrant.namespace.in_(('artifacts','logs')))):
            db.delete(assignment)
        db.commit()
    assert client.get(path,headers=as_('ana')).status_code==200
    assert client.get(path+'/logs',headers=as_('ana')).status_code==403
    assert client.get(path+'/outputs',headers=as_('ana')).status_code==403
    assert client.post(path+'/open',headers=as_('ana')).status_code==403
    me=client.get('/v1/me',headers=as_('ana')).json()
    assignment=client.post('/v1/permissions/assignments',headers=as_('admin'),json={
        'target_id':'run:'+run['id'],'namespace':'artifacts','role':'Reader',
        'layer_id':run['layer_id'],'principal_id':me['principal_id']})
    assert assignment.status_code==201,assignment.text
    assert client.get(path+'/outputs',headers=as_('ana')).status_code==200
    assert client.post(path+'/open',headers=as_('ana')).status_code==403
    own=client.get(path,headers=as_('ana')).json()
    assert 'artifacts.list' in own['capabilities'] and 'artifacts.read' not in own['capabilities']
    body={k:v for k,v in assignment.json().items() if k!='id'}
    body['role']='DataReader'
    client.put('/v1/permissions/assignments/'+assignment.json()['id'],headers=as_('admin'),json=body).raise_for_status()
    ticket=client.post(path+'/open',headers=as_('ana'))
    assert ticket.status_code==200,ticket.text
    from app.routes.gateway import _allowed
    with SessionLocal() as db:
        assert _allowed(db,'apps',run['id'],'ana',[])
    client.delete('/v1/permissions/assignments/'+assignment.json()['id'],headers=as_('admin')).raise_for_status()
    with SessionLocal() as db:
        assert not _allowed(db,'apps',run['id'],'ana',[])
    with SessionLocal() as db:
        for item in db.scalars(select(PermissionGrant).where(PermissionGrant.namespace=='runs')):
            db.delete(item)
        db.commit()
    assert client.post(path+'/stop',headers=as_('ana')).status_code==403


def test_repository_warehouse_and_secret_allowlists_follow_only_selected_group(client):
    from app import identity
    from app.models import GitRepository, ComputePool
    from app.routes.deploy import _can_deploy_from
    from app.routes.common import require_warehouse
    from fastapi import HTTPException
    import pytest
    _,alpha,beta=groups(client)
    select_group(client,'ana',alpha)
    with SessionLocal() as db:
        user=rbac.resolve_user(db,User('ana'))
        repo=GitRepository(name='isolated',url='https://example.com/repo.git',allowed_groups=[beta])
        assert not _can_deploy_from(repo,user,db)
        db.add(ComputePool(name='restricted',cpu=1,memory_gb=1,allowed_groups=[beta]));db.commit()
        with pytest.raises(HTTPException):
            require_warehouse(db,'restricted',user)
        assert not identity.can_access(db,'group',beta,user)
    select_group(client,'ana',beta)
    with SessionLocal() as db:
        user=rbac.resolve_user(db,User('ana'))
        repo=GitRepository(name='isolated',url='https://example.com/repo.git',allowed_groups=[beta])
        assert _can_deploy_from(repo,user,db)
        assert require_warehouse(db,'restricted',user).name=='restricted'
        assert identity.can_access(db,'group',beta,user)


def test_run_metadata_and_retry_are_separate_live_permissions(client):
    from .test_api import start_workspace
    from app.models import PermissionGrant
    ws=start_workspace(client)
    response=client.post('/v1/runs',headers=as_('ana'),json={
        'workspace_id':ws,'kind':'script','entrypoint':'task.py','pool':'small'})
    assert response.status_code==201,response.text
    run=response.json();path='/v1/runs/'+run['id']
    client.post(path+'/stop',headers=as_('ana')).raise_for_status()
    with SessionLocal() as db:
        for assignment in db.scalars(select(PermissionGrant).where(PermissionGrant.namespace=='runs')):
            db.delete(assignment)
        db.commit()
    assert client.get(path,headers=as_('ana')).status_code==403
    assert client.get('/v1/runs',headers=as_('ana')).json()==[]
    assert client.post(path+'/retry',headers=as_('ana')).status_code==403
    pid=client.get('/v1/me',headers=as_('ana')).json()['principal_id']
    grant=client.post('/v1/permissions/assignments',headers=as_('admin'),json={
        'target_id':'run:'+run['id'],'namespace':'runs','layer_id':run['layer_id'],
        'principal_id':pid,'role':'Reader'})
    assert grant.status_code==201,grant.text
    assert client.get(path,headers=as_('ana')).status_code==200
    assert client.post(path+'/retry',headers=as_('ana')).status_code==403
    from .rbac_helpers import workspace_permissions
    group=client.get('/v1/me',headers=as_('ana')).json()['active_group_id']
    workspace_permissions(client,group)
    retried=client.post(path+'/retry',headers=as_('ana'))
    assert retried.status_code==201,retried.text
    assert retried.json()['id']!=run['id']


def test_sql_result_cursors_cannot_be_reused_after_switching_groups(client):
    from app.models import SqlQuery
    from app.routes.sql import owned_query
    from fastapi import HTTPException
    import pytest
    _,alpha,beta=groups(client)
    select_group(client,'ana',alpha)
    with SessionLocal() as db:
        who=sql_access.actor(db,'Bearer ana')
        from app.models import utcnow
        from datetime import timedelta
        query=SqlQuery(principal_id=who.user.principal_id,actor=who.user.username,binding=who.binding,scope={},state='RUNNING',expires_at=utcnow()+timedelta(minutes=10))
        db.add(query);db.flush();ident=query.id;db.commit()
    select_group(client,'ana',beta)
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as denied:
            owned_query(db,ident,sql_access.actor(db,'Bearer ana'))
        assert denied.value.status_code==404
