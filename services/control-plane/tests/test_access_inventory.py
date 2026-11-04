"""Groups expose every category; data, configuration and delegation stay independent."""
from sqlalchemy import select
from app import permissions, rbac, compliance, layers
from app.db import SessionLocal
from app.models import Resource, RoleAssignment, PermissionGrant, ComplianceRole
from .conftest import as_, USERS
from .rbac_helpers import grant_group
from .test_products import setup_products
from .test_deploying import repo


def test_group_inventory_lists_each_role_once_with_its_permissions(client,repo):
    ids=setup_products(client,repo)
    group=grant_group(client,ids['public'],'Separated access','DataContributor',users=['bob'])
    with SessionLocal() as db:
        schema=db.get(Resource,ids['public'])
        db.add(RoleAssignment(resource_id=schema.parent_id,app_group_id=group,role='AccessAdministrator'))
        db.add(RoleAssignment(resource_id='workloads',app_group_id=group,role='Admin'))
        db.add(PermissionGrant(target_id='set-network',namespace='network.editor_allowlist',app_group_id=group,
            layer_id=layers.PROD,actions=['network.editor_allowlist.read']))
        db.add(ComplianceRole(app_group_id=group,role='compliance_reviewer',scope={'layers':[layers.PROD]}))
        db.commit()
    response=client.get('/v1/rbac/groups/'+group+'/access',headers=as_('admin'))
    assert response.status_code==200,response.text
    roles=response.json()['access_roles']
    by=lambda role:[r for r in roles if r['role']==role]
    data=by('DataContributor')
    assert len(data)==1 and data[0]['category']=='data' and data[0]['origin']=='object'
    keys={p['key'] for p in data[0]['permissions']}
    assert 'data.sql.select' in keys and not any(k.endswith('.roles.assign') for k in keys)
    # Admin is privileged, though it also reads data; each permission keeps its own category.
    admin=by('Admin')[0]
    assert admin['category']=='privileged'
    assert any(p['category']=='data' for p in admin['permissions'])
    assert by('AccessAdministrator')[0]['category']=='privileged'
    assert any(p['key'].endswith('.roles.assign') for p in by('AccessAdministrator')[0]['permissions'])
    named=by('Selected permissions')[0]
    assert named['category']=='platform' and named['layer_id']==layers.PROD and named['origin']=='named'
    assert [p['key'] for p in named['permissions']]==['network.editor_allowlist.read']
    duty=by('Forms reviewer')[0]
    assert duty['category']=='privileged' and duty['origin']=='duty' and duty['duty_id']
    assert [p['key'] for p in duty['permissions']]==['reviews.forms.review']
    assert duty['conditions']==['Layers: Prod']


def test_role_catalog_gives_each_role_a_category_and_its_permissions(client):
    response=client.get('/v1/permissions/catalog',headers=as_('bob'))
    assert response.status_code==200,response.text
    body=response.json()
    types={t['key']:t for t in body['types']}
    sql={r['name']:r for r in types['data-sql']['roles']}
    assert set(sql)=={'Reader','Contributor','DataReader','DataContributor','AccessAdministrator','Admin','Owner'}
    assert sql['DataReader']['category']=='data' and 'data.sql.select' in sql['DataReader']['permissions']
    assert sql['Reader']['category']=='platform' and 'data.sql.select' not in sql['Reader']['permissions']
    assert sql['Contributor']['category']=='platform' and 'data.sql.insert' not in sql['Contributor']['permissions']
    assert sql['Admin']['category']=='privileged' and sql['Owner']['category']=='privileged'
    workloads={r['name']:r for r in types['workloads']['roles']}
    assert workloads['Executor']['category']=='platform' and 'workloads.execution.run' in workloads['Executor']['permissions']
    assert 'workloads.execution.app.open' not in workloads['Executor']['permissions']
    assert 'workloads.execution.app.open' in workloads['DataReader']['permissions']
    assert workloads['Admin']['category']=='privileged'
    product={r['name']:r for r in types['product']['roles']}
    assert 'products.requests.own.create' in product['Viewer']['permissions'] and 'products.object.use' not in product['Viewer']['permissions']
    pages={r['name']:r for r in types['settings:adm-privacy']['roles']}
    assert 'pages.view' in pages['Reader']['permissions'] and 'compliance.forms.publish' in pages['Contributor']['permissions']
    assert {d['name'] for d in body['review_duties']}=={'Forms reviewer','Membership approver','Compliance administrator'}
    assert 'data.sql.select' in body['permissions'] and body['permissions']['data.sql.select']['category']=='data'
    assert body['custom_roles']==[]


def test_explicit_data_platform_and_access_roles_do_not_imply_each_other(client,repo):
    ids=setup_products(client,repo)
    group=grant_group(client,ids['public'],'Data only','DataContributor',users=['bob'])
    def check(role):
        # Roles add up, so each is checked alone.
        client.delete('/v1/rbac/resources/'+ids['public']+'/assignments/'+group,headers=as_('admin')).raise_for_status()
        client.put('/v1/rbac/resources/'+ids['public']+'/assignments',headers=as_('admin'),json={'app_group_id':group,'role':role}).raise_for_status()
        with SessionLocal() as db:
            user=rbac.resolve_user(db,USERS['bob']);resource=db.get(Resource,ids['public'])
            return {a:permissions.domain_allowed(db,user,a,resource,'assign' if a.endswith('.assign') else 'edit' if a.endswith('.edit') else 'read')
                for a in ('data.sql.select','data.sql.insert','storage.objects.metadata.edit','storage.access.roles.assign')}
    data=check('DataContributor')
    assert data['data.sql.select'] and data['data.sql.insert']
    assert not data['storage.objects.metadata.edit'] and not data['storage.access.roles.assign']
    platform=check('Contributor')
    assert platform['storage.objects.metadata.edit'] and not platform['data.sql.select'] and not platform['data.sql.insert']
    assert not platform['storage.access.roles.assign']
    privileged=check('AccessAdministrator')
    assert privileged['storage.access.roles.assign'] and not privileged['data.sql.select'] and not privileged['storage.objects.metadata.edit']


def test_ordinary_review_permission_respects_layer_and_does_not_grant_data(client):
    group=grant_group(client,'workloads','Scoped reviewers','Reader',users=['bob'])
    with SessionLocal() as db:
        db.add(PermissionGrant(target_id='cases',namespace='reviews',app_group_id=group,
            layer_id=layers.PROD,actions=['reviews.forms.review']))
        db.commit()
        user=rbac.resolve_user(db,USERS['bob'])
        assert compliance.has_role(db,user,'compliance_reviewer',layer_id=layers.PROD)
        assert not compliance.has_role(db,user,'compliance_reviewer',layer_id=layers.NON_PROD)


def test_access_administrator_can_delegate_without_acquiring_data(client,repo):
    ids=setup_products(client,repo)
    group=grant_group(client,ids['public'],'Access operators','AccessAdministrator',users=['bob'])
    target=client.post('/v1/rbac/groups',headers=as_('admin'),json={'name':'Delegated readers','owners':[client.get('/v1/me',headers=as_('admin')).json()['principal_id']]}).json()
    response=client.post('/v1/permissions/assignments',headers=as_('bob'),json={
        'target_id':ids['public'],'namespace':'data','role':'DataReader','app_group_id':target['id'],'layer_id':layers.NON_PROD})
    assert response.status_code==201,response.text
    with SessionLocal() as db:
        actor=rbac.resolve_user(db,USERS['bob'])
        assert not permissions.domain_allowed(db,actor,'data.sql.select',db.get(Resource,ids['public']),'read')


def test_access_administration_is_not_a_data_owner_review_exemption(client,repo):
    from .test_classification_options import classify
    from .test_compliance import set_up_policy
    ids=setup_products(client,repo)
    group=grant_group(client,ids['public'],'Access operators','AccessAdministrator',users=['bob'])
    classify(client,ids['sales'],{'ssn':{'classification':'restricted'}})
    set_up_policy(client,ids['public'])
    with SessionLocal() as db:
        resource=db.get(Resource,ids['public'])
        assert not compliance.owns(db,resource,group)
        assert compliance.role_requirement(db,resource,'DataReader',group)['policy'] is not None


def test_explicit_families_separate_payload_and_configuration_in_registry():
    from app.access_inventory import category
    for key,definition in permissions.REGISTRY.items():
        if 'PlatformContributor' in definition['roles']:
            assert category(key)=='platform',key
        if 'AccessAdministrator' in definition['roles']:
            assert category(key)!='data',key
        if 'DataContributor' in definition['roles']:
            assert category(key)!='privileged',key
    for key in ('workspaces.files.write','delegation.query.write','delegation.files.write'):
        assert 'DataContributor' in permissions.REGISTRY[key]['roles']
        assert 'PlatformContributor' not in permissions.REGISTRY[key]['roles']
