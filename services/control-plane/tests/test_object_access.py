"""Negative checks at the actual Trino policy/snapshot and catalog boundaries."""
import pytest
from sqlalchemy import select
from app import object_access, sql_access, rbac, catalog
from app.auth import User
from app.db import SessionLocal
from app.models import DataSource, Resource, SqlQuery, CatalogProduct, CatalogAsset
from .conftest import as_
from .rbac_helpers import integration, grant_group


@pytest.fixture
def objects(client, monkeypatch):
    monkeypatch.setattr(__import__('app.config', fromlist=['get_settings']).get_settings(), 'query_executor', 'trino')
    client.put('/v1/admin/datasources/object-lake', headers=as_('admin'), json=integration('object-lake','iceberg')).raise_for_status()
    schema = client.post('/v1/rbac/sources/object-lake/schemas', headers=as_('admin'), json={'name':'reporting'}).json()
    with SessionLocal() as db:
        row=db.get(Resource,schema['id']);row.config={**row.config,'state':'ready'};db.commit()
    def register(parent,kind,name):
        response=client.post('/v1/data-objects', headers=as_('admin'),json={'parent_id':parent,'kind':kind,'name':name})
        assert response.status_code==201,response.text
        return response.json()
    table=register(schema['id'],'table','sales')
    column=register(table['id'],'column','amount')
    hidden=register(table['id'],'column','personal_note')
    sibling=register(schema['id'],'table','payroll')
    return schema,table,column,hidden,sibling


def named(client,target,actions):
    pid=client.get('/v1/me',headers=as_('bob')).json()['principal_id']
    with SessionLocal() as db:
        from app.permissions import resource_layer
        layer=resource_layer(db,target)
    response=client.post('/v1/permissions/assignments',headers=as_('admin'),json={
        'target_id':target,'namespace':actions[0].rsplit('.',1)[0] if all(a.startswith(actions[0].rsplit('.',1)[0]+'.') for a in actions) else actions[0].split('.')[0], 'layer_id':layer,
        'principal_id':pid,'actions':actions})
    assert response.status_code==201,response.text
    return response.json()


def query():
    with SessionLocal() as db:
        user=rbac.resolve_user(db,User('bob'))
        return SqlQuery(scope=sql_access.scopes(db,sql_access.SqlActor(user,'test')))


def allow(q,operation,table='sales',columns=None):
    resource={'catalogName':'object_lake','schemaName':'reporting','tableName':table}
    if columns is not None:resource['columns']=columns
    return sql_access.policy_allow(q,{'operation':operation,'resource':{'table':resource}})


def test_single_table_select_does_not_grant_sibling_or_writes(client,objects):
    _,table,_,_,_=objects
    named(client,table['id'],['data.sql.select','data.queries.results.read'])
    q=query()
    assert allow(q,'SelectFromColumns',columns=['amount'])
    assert not allow(q,'SelectFromColumns','payroll',['salary'])
    for operation in ['InsertIntoTable','UpdateTableColumns','DeleteFromTable','CreateTable','DropTable','TruncateTable','ExecuteProcedure','UnknownOperation']:
        assert not allow(q,operation,columns=['amount'])
    assert not allow(q,'SelectFromColumns','sales$files',[])


def test_column_select_filters_metadata_and_rejects_star_or_join_escape(client,objects):
    _,_,column,_,_=objects
    named(client,column['id'],['data.sql.select','data.queries.results.read'])
    q=query()
    assert allow(q,'SelectFromColumns',columns=['amount'])
    assert not allow(q,'SelectFromColumns',columns=['amount','personal_note'])
    assert not allow(q,'SelectFromColumns','payroll',['amount'])
    assert allow(q,'FilterColumns',columns=['amount'])
    assert not allow(q,'FilterColumns',columns=['personal_note'])
    expression=sql_access.metadata_filters(q,{'catalogName':'system','schemaName':'metadata','tableName':'table_comments'})[0]['expression']
    assert "table_name IN ('sales')" in expression and 'payroll' not in expression


def test_independent_insert_update_delete_actions(client,objects):
    _,table,_,_,_=objects
    named(client,table['id'],['data.sql.insert'])
    q=query()
    assert allow(q,'InsertIntoTable')
    assert not allow(q,'SelectFromColumns',columns=['amount'])
    assert not allow(q,'UpdateTableColumns',columns=['amount'])
    assert not allow(q,'DeleteFromTable')


def test_column_update_requires_the_actual_column_shape(client,objects):
    _,_,column,_,_=objects
    named(client,column['id'],['data.sql.update'])
    q=query()
    assert allow(q,'UpdateTableColumns',columns=['amount'])
    assert not allow(q,'UpdateTableColumns',columns=['personal_note'])
    assert not allow(q,'UpdateTableColumns')
    assert not allow(q,'InsertIntoTable')


@pytest.mark.parametrize('role,select,insert', [('Viewer',False,False),('Executor',True,False),
    ('Contributor',False,True),('Manager',False,False),('Admin',True,True),('Owner',True,True)])
def test_all_six_object_roles_have_independent_meanings(client,objects,role,select,insert):
    _,table,_,_,_=objects
    grant_group(client,table['id'],'Scoped team',role,users=['bob'])
    q=query()
    assert allow(q,'SelectFromColumns',columns=['amount']) is select
    assert allow(q,'InsertIntoTable') is insert
    assert not allow(q,'SelectFromColumns','payroll',['salary'])


def test_live_revocation_and_cross_layer_grants_are_rechecked(client,objects):
    _,table,_,_,_=objects
    grant=named(client,table['id'],['data.sql.select','data.queries.results.read'])
    assert allow(query(),'SelectFromColumns',columns=['amount'])
    client.delete('/v1/permissions/assignments/'+grant['id'],headers=as_('admin')).raise_for_status()
    assert not allow(query(),'SelectFromColumns',columns=['amount'])
    with SessionLocal() as db:
        from app.permissions import allowed
        user=rbac.resolve_user(db,User('bob'))
        assert not allowed(db,user,'data.sql.select',table['id'],'1a0000000001')


def test_cached_browser_and_catalog_do_not_reveal_sibling_tables_or_columns(client,objects):
    schema,table,column,_,_=objects
    with SessionLocal() as db:
        parent=db.get(Resource,schema['id'])
        catalog.index_children(db,parent,[{'name':'sales','kind':'table'},{'name':'payroll','kind':'table'}])
        catalog.index_children(db,parent,[{'name':'amount','kind':'column'},{'name':'personal_note','kind':'column'}],'sales')
        db.commit()
    named(client,column['id'],['data.sql.select','data.queries.results.read'])
    named(client,column['id'],['storage.objects.list','storage.objects.describe'])
    response=client.get('/v1/data/schemas/'+schema['id']+'/children?cached=true',headers=as_('bob'))
    assert response.status_code==200,response.text
    assert [c['name'] for c in response.json()['children']]==['sales']
    response=client.get('/v1/data/schemas/'+schema['id']+'/children?cached=true&table=sales',headers=as_('bob'))
    assert [c['name'] for c in response.json()['children']]==['amount']
    with SessionLocal() as db:
        assets,_=catalog.visible_assets(db,rbac.resolve_user(db,User('bob')))
        assert 'payroll' not in {a.name for a in assets}
        asset=db.get(CatalogAsset,catalog.asset_id(schema['id'],'object:sales'))
        output=catalog.asset_out(db,asset,db.get(Resource,schema['id']),rbac.resolve_user(db,User('bob')))
        assert [c['name'] for c in output['attributes']['columns']]==['amount']


def test_registration_never_creates_a_data_owner_grant(client,objects):
    schema,table,_,_,_=objects
    assert client.get('/v1/data-objects/'+table['id'],headers=as_('bob')).status_code==404
    response=client.post('/v1/data-objects',headers=as_('bob'),json={'parent_id':schema['id'],'kind':'table','name':'unauthorized'})
    assert response.status_code==403
    response=client.delete('/v1/data-objects/'+table['id'],headers=as_('admin'))
    assert response.status_code==409  # Columns must be explicitly removed first.


def test_file_policy_is_exact_and_directory_policy_does_not_include_siblings():
    from app.object_access import file_session_policy, directory_list_allowed
    from fastapi import HTTPException
    source=DataSource(name='files',kind='s3',config={'bucket':'company-files','prefix':'base'})
    schema=Resource(name='namespace',kind='schema',config={})
    file=Resource(name='invoice.csv',kind='file',config={'path':'team/invoice.csv'})
    held={'data.files.read','data.files.list'}
    policy,path=file_session_policy(source,schema,file,held,'read')
    assert path=='base/namespace/team/invoice.csv'
    statements=policy['Statement']
    assert not any('s3:ListBucket' in s['Action'] for s in statements)
    assert next(s for s in statements if 's3:GetObject' in s['Action'])['Resource']==['arn:aws:s3:::company-files/base/namespace/team/invoice.csv']
    directory=Resource(name='team',kind='directory',config={'path':'team'})
    policy,_=file_session_policy(source,schema,directory,held,'read')
    assert next(s for s in policy['Statement'] if 's3:GetObject' in s['Action'])['Resource']==['arn:aws:s3:::company-files/base/namespace/team/*']
    with pytest.raises(HTTPException):
        file_session_policy(source,schema,file,{'data.files.create'},'write')
    with pytest.raises(HTTPException):
        file_session_policy(source,schema,file,held,'list')
    # A file's list metadata role must never authorize listing a similarly named directory.
    rules={'files':{'team/invoice.csv':{'directory':False,'actions':list(held)}}}
    assert not directory_list_allowed(rules,'team/invoice.csv')


def test_file_permission_target_is_canonical_from_metadata_and_explicit_registration(client):
    response=client.put('/v1/admin/datasources/object-files',headers=as_('admin'),json=integration('object-files','s3'))
    response.raise_for_status()
    schema=client.post('/v1/rbac/sources/object-files/schemas',headers=as_('admin'),json={'name':'documents'}).json()
    directory=client.post('/v1/data-objects',headers=as_('admin'),json={'parent_id':schema['id'],'kind':'directory','name':'team'}).json()
    file=client.post('/v1/data-objects',headers=as_('admin'),json={'parent_id':directory['id'],'kind':'file','name':'invoice.csv'}).json()
    with SessionLocal() as db:
        parent=db.get(Resource,schema['id'])
        asset=catalog.upsert(db,parent,'object:team/invoice.csv',kind='file',name='invoice.csv',attributes={'object_path':'team/invoice.csv'})
        target=object_access.target_for_asset(db,asset,parent,create=True)
        assert target.id==file['id']
        assert target.config['path']=='team/invoice.csv'


def test_data_product_grants_its_table_without_granting_schema_or_sibling(client,objects):
    schema,table,_,_,_=objects
    with SessionLocal() as db:
        parent=db.get(Resource,schema['id'])
        sales=catalog.upsert(db,parent,'object:sales',kind='table',name='sales',attributes={'object_path':'sales'})
        payroll=catalog.upsert(db,parent,'object:payroll',kind='table',name='payroll',attributes={'object_path':'payroll'})
        sales_id,payroll_id=sales.id,payroll.id
        db.commit()
    product=client.post('/v1/catalog/products',headers=as_('admin'),json={'title':'Sales only','asset_ids':[sales_id]})
    assert product.status_code==201,product.text
    with SessionLocal() as db:
        target=db.get(CatalogProduct,product.json()['id']).access_resource_id
    grant_group(client,target,'Product consumers','Consumer',users=['bob'])
    q=query()
    assert allow(q,'SelectFromColumns',columns=['amount'])
    assert not allow(q,'SelectFromColumns','payroll',['salary'])
    with SessionLocal() as db:
        source=db.get(DataSource,db.get(Resource,schema['id']).object_id)
        assert rbac.data_scope(db,rbac.resolve_user(db,User('bob')),source)=={}
    assert client.get('/v1/catalog/assets/'+sales_id,headers=as_('bob')).status_code==200
    assert client.get('/v1/catalog/assets/'+payroll_id,headers=as_('bob')).status_code==404


def test_broad_access_to_another_schema_does_not_expand_object_grants(client,objects):
    schema,table,_,_,_=objects
    named(client,schema['id'],['catalog.view'])
    other=client.post('/v1/rbac/sources/object-lake/schemas',headers=as_('admin'),json={'name':'other'}).json()
    with SessionLocal() as db:
        parent=db.get(Resource,schema['id'])
        sales=catalog.upsert(db,parent,'object:sales',kind='table',name='sales',attributes={'object_path':'sales'})
        hidden=catalog.upsert(db,parent,'object:payroll',kind='table',name='payroll',attributes={'object_path':'payroll'})
        sales_id,hidden_id=sales.id,hidden.id
        db.commit()
    group=grant_group(client,other['id'],'Other schema readers','Reader',users=['bob'])
    named(client,table['id'],['data.sql.select','data.queries.results.read'])
    assert client.get('/v1/catalog/assets/'+sales_id,headers=as_('bob')).status_code==200
    assert client.get('/v1/catalog/assets/'+hidden_id,headers=as_('bob')).status_code==404


def test_another_schemas_reader_grant_cannot_expand_column_metadata(client,objects):
    schema,_,column,_,_=objects
    named(client,schema['id'],['catalog.view'])
    other=client.post('/v1/rbac/sources/object-lake/schemas',headers=as_('admin'),json={'name':'other'}).json()
    grant_group(client,other['id'],'Other readers','Reader',users=['bob'])
    with SessionLocal() as db:
        parent=db.get(Resource,schema['id'])
        catalog.index_children(db,parent,[{'name':'sales','kind':'table'}])
        catalog.index_children(db,parent,[{'name':'amount','kind':'column'},
            {'name':'personal_note','kind':'column'}],'sales')
        ident=catalog.asset_id(schema['id'],'object:sales')
        db.commit()
    named(client,column['id'],['data.sql.select','data.queries.results.read'])
    response=client.get('/v1/catalog/assets/'+ident,headers=as_('bob'))
    assert response.status_code==200,response.text
    assert [c['name'] for c in response.json()['attributes']['columns']]==['amount']


def test_metric_dependencies_cannot_expose_a_hidden_sibling_table(client,objects):
    schema,table,_,_,_=objects
    with SessionLocal() as db:
        parent=db.get(Resource,schema['id'])
        hidden=catalog.upsert(db,parent,'object:payroll',kind='table',name='payroll',attributes={'object_path':'payroll'})
        metric=catalog.upsert(db,parent,'metric:private-payroll-count',kind='metric',name='Payroll count',
            attributes={'table':'sales','dependency_asset_ids':[hidden.id]})
        metric_id=metric.id
        db.commit()
    named(client,table['id'],['data.sql.select','data.queries.results.read'])
    with SessionLocal() as db:
        user=rbac.resolve_user(db,User('bob'))
        assets,_=catalog.visible_assets(db,user)
        assert metric_id not in {asset.id for asset in assets}
    assert client.get('/v1/catalog/assets/'+metric_id,headers=as_('bob')).status_code==404


def test_file_session_endpoint_passes_exact_policy_and_refuses_writes(client,monkeypatch):
    response=client.put('/v1/admin/datasources/scoped-files',headers=as_('admin'),json=integration('scoped-files','s3'))
    response.raise_for_status()
    schema=client.post('/v1/rbac/sources/scoped-files/schemas',headers=as_('admin'),json={'name':'documents'}).json()
    with SessionLocal() as db:
        row=db.get(Resource,schema['id']);row.config={**row.config,'state':'ready'};db.commit()
    file=client.post('/v1/data-objects',headers=as_('admin'),json={'parent_id':schema['id'],'kind':'file','name':'invoice.csv'}).json()
    named(client,file['id'],['data.files.read'])
    policies=[]
    def credentials(source,schemas,scope,store,**kwargs):
        policies.append(kwargs['policy_override'])
        return {'access_key_id':'session','secret_access_key':'secret','session_token':'token'},{}
    import app.storage_access
    monkeypatch.setattr(app.storage_access,'credentials',credentials)
    path='/v1/data-objects/'+file['id']+'/session'
    response=client.post(path,headers=as_('bob'),json={'mode':'read'})
    assert response.status_code==200,response.text
    assert response.json()['config']['prefix']=='documents/invoice.csv'
    assert next(s for s in policies[0]['Statement'] if 's3:GetObject' in s['Action'])['Resource']==['arn:aws:s3:::scoped-files/documents/invoice.csv']
    assert not any('s3:ListBucket' in s['Action'] for s in policies[0]['Statement'])
    for mode in ('write','delete','list','read_write'):
        assert client.post(path,headers=as_('bob'),json={'mode':mode}).status_code==403
    assert len(policies)==1


@pytest.mark.parametrize('role,select,insert',[
    ('Viewer',False,False),('Executor',True,False),('Contributor',False,True),
    ('Manager',False,False),('Admin',True,True),('Owner',True,True),
])
def test_schema_roles_keep_metadata_separate_from_payload(client,objects,role,select,insert):
    schema,_,_,_,_=objects
    grant_group(client,schema['id'],'Schema team',role,users=['bob'])
    q=query()
    assert allow(q,'SelectFromColumns',columns=['amount']) is select
    assert allow(q,'InsertIntoTable') is insert
    if role in ('Viewer','Manager'):
        with SessionLocal() as db:
            resource=db.get(Resource,schema['id'])
            user=rbac.resolve_user(db,User('bob'))
            assert rbac.allowed(db,user,'read',resource)
            assert not rbac.data_scope(db,user,db.get(DataSource,resource.object_id))
