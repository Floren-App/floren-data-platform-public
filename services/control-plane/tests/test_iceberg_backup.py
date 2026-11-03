"""Real Iceberg/Parquet recovery, catalog protocol and dependency-closure failures."""
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlsplit,unquote

import httpx
import pyarrow as pa
from pyarrow.fs import SubTreeFileSystem
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.io.pyarrow import PyArrowFileIO,PyArrowLocalFileSystem
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField,LongType,StringType
import pytest
from botocore.exceptions import ClientError

from app import backup_adapters,iceberg_backup as iceberg,iceberg_recovery,storage_recovery,deps,recovery_archive
from app.db import SessionLocal
from app.models import DataSource
from .test_recovery import recovery_keys


class MappedFileIO(PyArrowFileIO):
    """Keep real S3 URIs in Iceberg metadata; use isolated local bytes in this test."""
    def new_output(self,location):
        uri=urlsplit(location)
        (Path(self.properties['test-root'])/uri.netloc/uri.path.lstrip('/')).parent.mkdir(parents=True,exist_ok=True)
        return super().new_output(location)
    def _initialize_fs(self,scheme,netloc=None):
        assert scheme=='s3'
        return SubTreeFileSystem(self.properties['test-root'],PyArrowLocalFileSystem())


def directory(root,name):
    root.mkdir(exist_ok=True)
    return SqlCatalog(name,uri='sqlite:///'+str(root/(name+'.db')),warehouse='s3://lake/warehouse',
        **{'py-io-impl':__name__+'.MappedFileIO','test-root':str(root)})


class Body(io.BytesIO):
    def iter_chunks(self,chunk_size):return iter(lambda:self.read(chunk_size),b'')


class Objects:
    def __init__(self,root):self.root=root;self.writes=[]
    def path(self,bucket,key):return self.root/bucket/key
    def data(self,bucket,key):
        try:return self.path(bucket,key).read_bytes()
        except FileNotFoundError:raise ClientError({'Error':{'Code':'NoSuchKey'}},'GetObject')
    def head_object(self,Bucket,Key):
        data=self.data(Bucket,Key);return {'ContentLength':len(data),'ETag':hashlib.sha256(data).hexdigest()}
    def get_object(self,Bucket,Key,IfMatch=None):
        data=self.data(Bucket,Key)
        if IfMatch:assert IfMatch==hashlib.sha256(data).hexdigest()
        return {'Body':Body(data),'VersionId':'original-version'}
    def put_object(self,Bucket,Key,Body,IfNoneMatch,**kwargs):
        assert IfNoneMatch=='*'
        path=self.path(Bucket,Key);path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as out:out.write(Body.read() if hasattr(Body,'read') else Body)
        self.writes.append((Bucket,Key))


@pytest.fixture
def lake(client,tmp_path,monkeypatch):
    source=directory(tmp_path/'original','original');source.create_namespace('analytics');source.create_namespace('empty')
    table=source.create_table('analytics.orders',Schema(NestedField(1,'id',LongType(),required=False),NestedField(2,'name',StringType(),required=False)),
        properties={'format-version':'2'})
    table.append(pa.table({'id':pa.array([1],pa.int64()),'name':['first']}));first=table.current_snapshot().snapshot_id
    table.manage_snapshots().create_tag(first,'retained').commit()
    table.append(pa.table({'id':pa.array([2],pa.int64()),'name':['second']}))
    source.create_table('analytics.blank',table.schema())
    current={'catalog':source};requests=[]
    def handler(request):
        assert request.headers.get('authorization')=='Bearer vault-only-catalog-token'
        assert request.url.host=='catalog.example';requests.append(str(request.url))
        path=unquote(request.url.path);catalog=current['catalog']
        def response(table):return {'metadata-location':table.metadata_location,'metadata':json.loads(table.metadata.model_dump_json(by_alias=True)),
            'config':{'token':'VENDORED-CREDENTIAL-MUST-NOT-BE-ARCHIVED'}}
        if path=='/v1/config':return httpx.Response(200,json={'overrides':{'prefix':'demo','token':'NEVER-ARCHIVE-CONFIG'}})
        if path=='/v1/demo/namespaces':
            if request.method=='POST':catalog.create_namespace(tuple(json.loads(request.content)['namespace']));return httpx.Response(200,json={})
            parent=tuple(request.url.params['parent'].split('\x1f')) if request.url.params.get('parent') else ()
            return httpx.Response(200,json={'namespaces':[list(n) for n in catalog.list_namespaces(parent)]})
        parts=path.split('/');ns=tuple(parts[4].split('\x1f'))
        if len(parts)==5:
            if ns in catalog.list_namespaces(ns[:-1]):return httpx.Response(200,json={'namespace':list(ns),'properties':{}})
            return httpx.Response(404,json={})
        if parts[5]=='register':
            data=json.loads(request.content);t=catalog.register_table(ns+(data['name'],),data['metadata-location']);return httpx.Response(200,json=response(t))
        if len(parts)==6:
            identifiers=catalog.list_tables(ns)
            # Exercise actual REST pagination and explicit identifier namespaces.
            offset=int(request.url.params.get('pageToken','0'))
            page=identifiers[offset:offset+1]
            return httpx.Response(200,json={'identifiers':[{'namespace':list(i[:-1]),'name':i[-1]} for i in page],
                **({'next-page-token':str(offset+1)} if offset+1<len(identifiers) else {})})
        try:t=catalog.load_table(ns+(parts[6],))
        except Exception:return httpx.Response(404,json={})
        return httpx.Response(200,json=response(t))
    original_client=httpx.Client
    monkeypatch.setattr(iceberg.httpx,'Client',lambda *args,**kw:original_client(*args,**kw,transport=httpx.MockTransport(handler)))
    objects=Objects(tmp_path/'original')
    monkeypatch.setattr(iceberg,'object_client',lambda *args:objects)
    deps.get_secret_store().put('datasources/lake/provisioner',{'access_key_id':'vault-key','secret_access_key':'vault-secret',
        'catalog_token':'vault-only-catalog-token'})
    with SessionLocal() as db:
        item=DataSource(name='lake',kind='iceberg',config={'warehouse':'s3://lake/warehouse','catalog_uri':'https://catalog.example'})
        db.add(item);db.commit()
        frozen={'id':item.id,'name':item.name,'kind':item.kind,'config':item.config,'schemas':[],
            'source_prefix':'','max_bytes':16*1024*1024,'max_objects':1000}
    return {'frozen':frozen,'objects':objects,'catalog':source,'table':table,'first':first,'current':current,'requests':requests}


def archive(lake,tmp_path):
    root=tmp_path/'export';root.mkdir();tar=tmp_path/'storage.tar'
    with SessionLocal() as db:backup_adapters.bundle(db,lake['frozen'],deps.get_secret_store(),root,tar)
    output=tmp_path/'verified';output.mkdir()
    manifest=storage_recovery.unpack(tar,output)
    return tar,output,manifest


def test_real_iceberg_encrypted_backup_restore_keeps_snapshots_tags_and_rows(lake,tmp_path,monkeypatch,recovery_keys):
    tar,_,manifest=archive(lake,tmp_path)
    encrypted=tmp_path/'backup.fdp';clear=tmp_path/'authenticated.tar'
    recovery_archive.encrypt(tar,encrypted,recovery_keys[1]);recovery_archive.decrypt(encrypted,clear,recovery_keys[0],recovery_archive.checksum(encrypted))
    restored=tmp_path/'decrypted';restored.mkdir();storage_recovery.unpack(clear,restored)
    assert manifest['method']=='iceberg-snapshot-files'
    assert ['empty'] in manifest['namespaces']
    orders=next(t for t in manifest['tables'] if t['name']=='orders')
    assert orders['current_snapshot_id']!=lake['first'] and lake['first'] in orders['snapshot_ids']
    for secret in ('vault-secret','vault-key','vault-only-catalog-token','VENDORED-CREDENTIAL','NEVER-ARCHIVE-CONFIG'):
        assert secret.encode() not in tar.read_bytes()
    target=directory(tmp_path/'recovered','recovered');lake['current']['catalog']=target
    objects=Objects(tmp_path/'recovered');monkeypatch.setattr(iceberg,'object_client',lambda *args:objects)
    with SessionLocal() as db:
        source=db.get(DataSource,lake['frozen']['id'])
        result=iceberg_recovery.restore(db,source,deps.get_secret_store(),restored)
        # Repeating recovery is safe and reuses identical objects/catalog entries.
        repeated=iceberg_recovery.restore(db,source,deps.get_secret_store(),restored)
    assert result==repeated and result['restored_tables']==2
    table=target.load_table('analytics.orders')
    assert sorted(table.scan().to_arrow().to_pylist(),key=lambda r:r['id'])==[{'id':1,'name':'first'},{'id':2,'name':'second'}]
    assert table.scan(snapshot_id=lake['first']).to_arrow().to_pylist()==[{'id':1,'name':'first'}]
    assert table.metadata.refs['retained'].snapshot_id==lake['first']
    assert target.load_table('analytics.blank').scan().to_arrow().num_rows==0
    assert ('empty',) in target.list_namespaces()


def test_missing_snapshot_file_never_produces_success(lake,tmp_path):
    file=next((tmp_path/'original'/'lake').rglob('*.parquet'));file.unlink()
    with pytest.raises(ClientError):archive(lake,tmp_path)


def test_namespace_selection_is_exact_and_missing_namespace_is_rejected(lake,tmp_path):
    lake['frozen']['schemas']=['empty'];_,_,manifest=archive(lake,tmp_path)
    assert manifest['tables']==[] and manifest['objects']==[] and manifest['namespaces']==[['empty']]
    directory=iceberg.RestCatalog(type('Source',(),{'config':lake['frozen']['config'],'name':'lake'})(),deps.get_secret_store())
    try:
        with pytest.raises(ValueError,match='not found'):directory.tables(['missing'],100)
    finally:directory.close()


def test_recovery_checks_dependency_closure_before_any_target_access(lake,tmp_path,monkeypatch):
    _,root,manifest=archive(lake,tmp_path)
    # A valid checksum inventory that omits a manifest/data dependency still fails.
    manifest['objects']=[o for o in manifest['objects'] if not o['key'].endswith('.parquet')]
    (root/'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(iceberg,'object_client',lambda *args:pytest.fail('target contacted before validation'))
    with SessionLocal() as db,pytest.raises(KeyError):
        iceberg_recovery.restore(db,db.get(DataSource,lake['frozen']['id']),deps.get_secret_store(),root)


def test_recovery_refuses_conflicting_files_and_catalog_entries(lake,tmp_path,monkeypatch):
    _,root,manifest=archive(lake,tmp_path)
    target=directory(tmp_path/'recovered','recovered');lake['current']['catalog']=target
    objects=Objects(tmp_path/'recovered');monkeypatch.setattr(iceberg,'object_client',lambda *args:objects)
    first=manifest['objects'][0];path=objects.path(first['bucket'],first['key']);path.parent.mkdir(parents=True);path.write_bytes(b'conflicting data')
    with SessionLocal() as db,pytest.raises(ValueError,match='conflicts'):
        iceberg_recovery.restore(db,db.get(DataSource,lake['frozen']['id']),deps.get_secret_store(),root)
    assert not objects.writes and not target.list_namespaces()
    path.unlink();target.create_namespace('analytics')
    target.create_table('analytics.orders',lake['table'].schema())
    with SessionLocal() as db,pytest.raises(ValueError,match='replace'):
        iceberg_recovery.restore(db,db.get(DataSource,lake['frozen']['id']),deps.get_secret_store(),root)
    assert not objects.writes


@pytest.mark.parametrize('uri',['s3://other/warehouse/data.parquet','https://evil.example/data',
    's3://lake/other/data','s3://lake/warehouse/../data','s3://lake/warehouse/data?token=x'])
def test_snapshot_files_cannot_escape_warehouse(uri):
    with pytest.raises(ValueError):iceberg.location(uri,'s3://lake/warehouse')


def test_live_delete_files_statistics_and_v1_manifests_are_included(tmp_path):
    from fastavro import writer
    root=tmp_path/'original';root.mkdir();objects=Objects(root);base='s3://lake/warehouse/'
    def put(name,data):
        path=root/'lake'/'warehouse'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);return base+name
    def avro(name,rows):
        schema={'type':'record','name':'entry','fields':[{'name':'status','type':'int'},
            {'name':'data_file','type':{'type':'record','name':'file','fields':[{'name':'file_path','type':'string'},{'name':'content','type':'int'}]}}]}
        out=io.BytesIO();writer(out,schema,rows);return put(name,out.getvalue())
    data=put('data.parquet',b'opaque data file');delete=put('delete.parquet',b'opaque equality/position deletes')
    stats=put('stats.puffin',b'statistics');partstats=put('partstats.parquet',b'partition stats')
    manifest=avro('manifest.avro',[{'status':1,'data_file':{'file_path':data,'content':0}},
        {'status':0,'data_file':{'file_path':delete,'content':2}},
        {'status':2,'data_file':{'file_path':'s3://lake/warehouse/expired-and-gone.parquet','content':0}}])
    metadata={'format-version':1,'table-uuid':'a-table-uuid','location':base+'table',
        'current-snapshot-id':1,'snapshots':[{'snapshot-id':1,'manifests':[manifest]}],
        'statistics':[{'statistics-path':stats}],'partition-statistics':[{'statistics-path':partstats}]}
    uri=put('metadata.json',json.dumps(metadata).encode())
    output=tmp_path/'export';output.mkdir();capture=iceberg.Export(objects,base,output,1024*1024,100)
    table=capture.table({'namespace':['analytics'],'name':'orders','metadata_location':uri})
    assert table['snapshot_ids']==[1]
    assert {o['uri'] for o in capture.objects.values()}=={uri,manifest,data,delete,stats,partstats}


def test_unsupported_format_and_size_limits_fail_before_publication(lake,tmp_path):
    lake['frozen']['max_bytes']=1
    with pytest.raises(ValueError,match='size limit'):archive(lake,tmp_path)
    path=next((tmp_path/'original'/'lake').rglob('*.metadata.json'))
    metadata=json.loads(path.read_text());metadata['format-version']=3;path.write_text(json.dumps(metadata))
    output=tmp_path/'other-export';output.mkdir()
    capture=iceberg.Export(lake['objects'],'s3://lake/warehouse',output,1024*1024,100)
    uri='s3://lake/'+str(path.relative_to(tmp_path/'original'/'lake'))
    with pytest.raises(ValueError,match='v1/v2'):capture.table({'namespace':['analytics'],'name':'orders','metadata_location':uri})


def test_plan_worker_records_verified_iceberg_archive_and_missing_file_failure(client,lake,tmp_path,monkeypatch,recovery_keys):
    from app import backups
    from .test_backup_plans import destination,ADMIN
    objects={}
    class Destination:
        def upload_file(self,path,bucket,key,ExtraArgs):objects[(bucket,key)]=Path(path).read_bytes()
        def get_object(self,Bucket,Key):return {'Body':Body(objects[(Bucket,Key)]),'VersionId':'verified-archive-version'}
    monkeypatch.setattr(backups,'client',lambda *args,**kwargs:Destination())
    dest=destination(client,recovery_keys)
    plan=client.post('/v1/backups/plans',headers=ADMIN,json={'name':'Lake snapshots','source_kind':'storage',
        'source_id':lake['frozen']['id'],'destination_id':dest['id']})
    assert plan.status_code==201,plan.text
    ident=plan.json()['id']
    queued=client.post('/v1/backups/plans/'+ident+'/runs',headers=ADMIN);assert queued.status_code==202,queued.text
    backups.run_one(SessionLocal,deps.get_secret_store())
    history=client.get('/v1/backups/plans/'+ident+'/runs',headers=ADMIN).json()
    assert history[0]['status']=='succeeded' and history[0]['source_kind']=='iceberg' and history[0]['sha256']
    first_id=history[0]['id']
    next((tmp_path/'original'/'lake').rglob('*.parquet')).unlink()
    client.post('/v1/backups/plans/'+ident+'/runs',headers=ADMIN).raise_for_status()
    backups.run_one(SessionLocal,deps.get_secret_store())
    history=client.get('/v1/backups/plans/'+ident+'/runs',headers=ADMIN).json()
    assert history[0]['status']=='failed' and not history[0]['sha256']
    assert next(r for r in history if r['id']==first_id)['status']=='succeeded'


def test_managed_catalog_uses_existing_layer_secrets_and_derived_endpoints(client,monkeypatch):
    from app import layers
    from app.models import Resource
    from app.layer_infrastructure import InfrastructureSpec,namespace_for
    from app.config import get_settings
    import boto3
    spec=InfrastructureSpec.model_validate({'enabled':True,'trino':{'workers':{'warehouse':'trino'}},
        'postgres':{'enabled':True},'rustfs':{'enabled':True,'image':'rustfs/rustfs:1.0.2','buckets':['lake']},'iceberg':{'enabled':True,'catalog':'lake','bucket':'lake'}})
    reads=[];s3_args=[];jdbc=[]
    def read(namespace,name,key):reads.append((namespace,name,key));return {'access-key':'existing-key','secret-key':'existing-secret','engine-password':'existing-password'}[key]
    monkeypatch.setattr(deps.get_orchestrator(),'read_layer_secret',read)
    monkeypatch.setattr(boto3,'client',lambda kind,**kwargs:s3_args.append(kwargs) or object())
    monkeypatch.setattr(iceberg,'JdbcCatalog',lambda url,name:jdbc.append((url,name)) or object())
    with SessionLocal() as db:
        layer=db.get(Resource,layers.NON_PROD);layer.config={**layer.config,'infrastructure':spec.model_dump(mode='json')}
        source=DataSource(name='managed-lake',kind='iceberg',config={'catalog':'lake','warehouse':'s3://lake/','layer_id':layers.NON_PROD})
        iceberg.object_client(db,source,deps.get_secret_store());iceberg.catalog(db,source,deps.get_secret_store())
        namespace=namespace_for(layer.id,get_settings().layer_namespace_prefix or 'fdp-layer')
    assert s3_args[0]['endpoint_url']=='http://fdp-rustfs.'+namespace+'.svc:9000'
    assert s3_args[0]['aws_secret_access_key']=='existing-secret'
    assert jdbc[0][0].host=='fdp-postgres.'+namespace+'.svc' and jdbc[0][0].password=='existing-password'
    assert {key for _,_,key in reads}=={'access-key','secret-key','engine-password'}


def test_multipart_recovery_uses_conditional_s3_publication_and_readback(tmp_path):
    import boto3
    from moto import mock_aws
    # Exercise the real SDK/multipart API, rather than a hand-written multipart stub.
    path=tmp_path/'large-object';size=65*1024*1024
    with path.open('wb') as stream:stream.truncate(size)
    obj={'bucket':'iceberg-recovery','key':'large.parquet','size_bytes':size,
        'sha256':recovery_archive.checksum(path),'content_type':'application/octet-stream'}
    with mock_aws():
        s3=boto3.client('s3',region_name='us-east-1');s3.create_bucket(Bucket=obj['bucket'])
        iceberg_recovery.upload_new(s3,path,obj)
        assert iceberg_recovery.verify_existing(s3,obj['bucket'],obj['key'],obj)
        s3.put_object(Bucket=obj['bucket'],Key=obj['key'],Body=b'conflicting data')
        with pytest.raises(ValueError,match='conflicts'):iceberg_recovery.upload_new(s3,path,obj)
        assert s3.get_object(Bucket=obj['bucket'],Key=obj['key'])['Body'].read()==b'conflicting data'
