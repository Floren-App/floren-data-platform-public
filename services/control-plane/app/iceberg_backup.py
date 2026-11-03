"""Snapshot-aware Iceberg v1/v2 exports; catalog auth and object keys stay in SecretStore.

Every retained snapshot in the captured metadata is traversed. Missing dependencies
fail the export; concurrent writes never change the metadata tree being copied.
"""
import gzip
import hashlib
import json
import os
import time
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from . import backups
from .models import Resource


def location(uri, warehouse):
    parsed, base = urlsplit(uri), urlsplit(warehouse)
    if (parsed.scheme not in ('s3', 's3a') or base.scheme not in ('s3','s3a') or
            not parsed.netloc or parsed.netloc != base.netloc or parsed.query or parsed.fragment or
            parsed.username or parsed.password):
        raise ValueError('Iceberg file must be inside the integration warehouse')
    key, prefix = parsed.path.lstrip('/'), base.path.strip('/')
    if not key or any(part in ('.','..','') for part in key.split('/')) or any(c in key for c in '*?\x00'):
        raise ValueError('invalid Iceberg object location')
    if prefix and not key.startswith(prefix+'/'):
        raise ValueError('Iceberg file escaped the integration warehouse')
    return parsed.netloc, key


def managed(db, source):
    """Resolve the same managed JDBC catalog binding used by Trino, not caller flags."""
    from .source_catalogs import layer_bound_catalogs
    from .layer_infrastructure import InfrastructureSpec, namespace_for
    from .layer_manifests import with_credentials
    from .config import get_settings
    layer_id=source.config.get('layer_id')
    layer = db.get(Resource,layer_id) if layer_id else None
    if not layer or source.config.get('catalog') not in layer_bound_catalogs(layer):
        return None
    spec = with_credentials(InfrastructureSpec.model_validate(layer.config['infrastructure']))
    if not spec.enabled or spec.retired or not spec.iceberg.enabled or source.config['catalog'] != spec.iceberg.catalog:
        raise ValueError('managed Iceberg catalog is not active')
    if source.config['warehouse'].rstrip('/') != 's3://'+spec.iceberg.bucket:
        raise ValueError('managed Iceberg warehouse does not match its layer')
    return spec, namespace_for(layer.id,get_settings().layer_namespace_prefix or 'fdp-layer')


def object_client(db, source, store):
    import boto3
    from botocore.config import Config
    binding = managed(db,source)
    if binding:
        from .deps import get_orchestrator
        spec,namespace = binding
        reader=get_orchestrator().read_layer_secret
        secret={'access_key_id':reader(namespace,spec.rustfs.credentials_secret,'access-key'),
                'secret_access_key':reader(namespace,spec.rustfs.credentials_secret,'secret-key')}
        endpoint=('%s://fdp-rustfs.%s.svc:9000' % ('https' if spec.rustfs.tls.mode!='disabled' else 'http',namespace))
    else:
        secret = store.read('datasources/'+source.name+'/provisioner') or store.read('datasources/'+source.name)
        endpoint=source.config.get('endpoint_url')
    if not secret.get('access_key_id') or not secret.get('secret_access_key'):
        raise ValueError('Iceberg backup storage credentials unavailable in the configured vault')
    return boto3.client('s3',endpoint_url=endpoint or None,region_name=source.config.get('region','us-east-1'),
        verify=os.environ.get('SSL_CERT_FILE') or True,aws_access_key_id=secret['access_key_id'],
        aws_secret_access_key=secret['secret_access_key'],aws_session_token=secret.get('session_token'),
        config=Config(connect_timeout=10,read_timeout=120,retries={'max_attempts':2},s3={'addressing_style':'path'}))


class RestCatalog:
    def __init__(self, source, store):
        cfg=source.config;parsed=urlsplit(cfg['catalog_uri'])
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid Iceberg REST catalog origin')
        secret=store.read('datasources/'+source.name+'/provisioner') or store.read('datasources/'+source.name)
        headers={'Authorization':'Bearer '+secret['catalog_token']} if secret.get('catalog_token') else {}
        self.deadline=time.monotonic()+7100
        self.http=httpx.Client(base_url=cfg['catalog_uri'].rstrip('/')+'/',headers=headers,
            timeout=30,follow_redirects=False,verify=os.environ.get('SSL_CERT_FILE') or True)
        try:
            config=self.request('GET','v1/config',params={'warehouse':cfg['warehouse']})
            props={**config.get('defaults',{}),**config.get('overrides',{})}
            prefix=props.get('prefix','')
            if prefix and (not isinstance(prefix,str) or any(part in ('','..','.') for part in prefix.split('/'))):
                raise ValueError('invalid catalog prefix')
            self.base='v1/'+(quote(prefix,safe='/')+'/' if prefix else '')
        except Exception:
            self.close();raise

    def request(self, method, path, **kwargs):
        # Bound JSON responses as well as S3 objects. Never store response config/vended credentials.
        if time.monotonic()>self.deadline:raise TimeoutError('catalog backup window exceeded')
        with self.http.stream(method,path,**kwargs) as response:
            response.raise_for_status();payload=bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic()>self.deadline:raise TimeoutError('catalog backup window exceeded')
                payload.extend(chunk)
                if len(payload)>64*1024*1024:raise ValueError('catalog response exceeds limit')
            return json.loads(payload) if payload else {}

    def pages(self,path,key,**params):
        seen=set();token=None;count=0
        while True:
            count+=1
            if count>10000:raise ValueError('catalog pagination exceeds limit')
            result=self.request('GET',path,params={**params,**({'pageToken':token} if token else {}),'pageSize':100})
            for item in result.get(key,[]):yield item
            token=result.get('next-page-token')
            if not token:return
            if token in seen:raise ValueError('catalog pagination repeated a token')
            seen.add(token)

    def tables(self,schemas,limit):
        pending=[()];seen=set();discovered={()};found=[];namespaces=[]
        while pending:
            parent=pending.pop()
            if parent in seen:continue
            seen.add(parent)
            if len(seen)>limit:raise ValueError('catalog exceeds configured object limit')
            children=self.pages(self.base+'namespaces','namespaces',**({'parent':'\x1f'.join(parent)} if parent else {}))
            for child in children:
                namespace=tuple(child)
                if not namespace or any(not isinstance(p,str) or not p for p in namespace):raise ValueError('invalid namespace')
                if namespace in discovered:continue
                discovered.add(namespace)
                if len(discovered)>limit+1:raise ValueError('catalog exceeds configured namespace limit')
                pending.append(namespace)
                if schemas and '.'.join(namespace) not in schemas:continue
                namespaces.append(list(namespace))
                path=self.base+'namespaces/'+quote('\x1f'.join(namespace),safe='')+'/tables'
                for identifier in self.pages(path,'identifiers'):
                    if tuple(identifier['namespace'])!=namespace:raise ValueError('catalog identifier escaped namespace')
                    name=identifier['name']
                    loaded=self.request('GET',path+'/'+quote(name,safe=''))
                    found.append({'namespace':list(namespace),'name':name,'metadata_location':loaded['metadata-location']})
                    if len(found)>limit:raise ValueError('catalog exceeds configured table limit')
        if schemas and set(schemas)-{'.'.join(n) for n in namespaces}:raise ValueError('selected Iceberg namespace not found')
        return found,namespaces

    def check(self,tables):
        for table in tables:
            path=self.base+'namespaces/'+quote('\x1f'.join(table['namespace']),safe='')+'/tables/'+quote(table['name'],safe='')
            try:existing=self.request('GET',path)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code==404:continue
                raise
            if existing['metadata-location']!=table['metadata_location']:raise ValueError('restore would replace an existing Iceberg table')

    def register(self,tables,namespaces):
        # Only create missing entries. A conflicting table is never replaced.
        for ns in sorted(namespaces,key=lambda n:(len(n),n)):
            path=self.base+'namespaces/'+quote('\x1f'.join(ns),safe='')
            try:self.request('GET',path)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code!=404:raise
                try:self.request('POST',self.base+'namespaces',json={'namespace':ns,'properties':{}})
                except httpx.HTTPStatusError as race:
                    if race.response.status_code!=409:raise
        for table in tables:
            path=self.base+'namespaces/'+quote('\x1f'.join(table['namespace']),safe='')
            try:existing=self.request('GET',path+'/tables/'+quote(table['name'],safe=''))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code!=404:raise
                self.request('POST',path+'/register',json={'name':table['name'],'metadata-location':table['metadata_location']})
                continue
            if existing['metadata-location']!=table['metadata_location']:raise ValueError('restore would replace an existing Iceberg table')

    def close(self):self.http.close()


class JdbcCatalog:
    def __init__(self,url,name):
        self.engine=create_engine(url);self.name=name

    def tables(self,schemas,limit):
        with self.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            connection.execute(text('SET TRANSACTION READ ONLY'))
            rows=connection.execute(text("SELECT table_namespace,table_name,metadata_location FROM iceberg_tables "
                "WHERE catalog_name=:name AND (iceberg_type IS NULL OR iceberg_type='TABLE') ORDER BY table_namespace,table_name LIMIT :limit"),
                {'name':self.name,'limit':limit+1}).all()
            names=list(connection.execute(text('SELECT DISTINCT namespace FROM iceberg_namespace_properties WHERE catalog_name=:name LIMIT :limit'),
                {'name':self.name,'limit':limit+1}).scalars())
        if len(rows)>limit or len(names)>limit:raise ValueError('catalog exceeds configured object limit')
        namespaces=sorted(set(names)|{row[0] for row in rows})
        if schemas and set(schemas)-set(namespaces):raise ValueError('selected Iceberg namespace not found')
        return ([{'namespace':ns.split('.'),'name':name,'metadata_location':loc} for ns,name,loc in rows if not schemas or ns in schemas],
                [ns.split('.') for ns in namespaces if not schemas or ns in schemas])

    def check(self,tables):
        if any('.' in part for table in tables for part in table['namespace']):
            raise ValueError('JDBC recovery cannot represent literal dots in namespace components')
        with self.engine.connect() as connection:
            for table in tables:
                existing=connection.execute(text('SELECT metadata_location,iceberg_type FROM iceberg_tables WHERE catalog_name=:catalog AND table_namespace=:ns AND table_name=:name'),
                    {'catalog':self.name,'ns':'.'.join(table['namespace']),'name':table['name']}).first()
                if existing and (existing[0]!=table['metadata_location'] or existing[1] not in (None,'TABLE')):
                    raise ValueError('restore would replace an existing Iceberg table')

    def register(self,tables,namespaces):
        with self.engine.begin() as connection:
            for ns in namespaces:
                connection.execute(text("INSERT INTO iceberg_namespace_properties(catalog_name,namespace,property_key,property_value) "
                    "VALUES (:catalog,:namespace,'exists','true') ON CONFLICT DO NOTHING"),{'catalog':self.name,'namespace':'.'.join(ns)})
            for table in tables:
                args={'catalog':self.name,'ns':'.'.join(table['namespace']),'name':table['name'],'loc':table['metadata_location']}
                connection.execute(text("INSERT INTO iceberg_tables(catalog_name,table_namespace,table_name,metadata_location,iceberg_type) "
                    "VALUES (:catalog,:ns,:name,:loc,'TABLE') ON CONFLICT DO NOTHING"),args)
                existing=connection.execute(text('SELECT metadata_location,iceberg_type FROM iceberg_tables WHERE catalog_name=:catalog AND table_namespace=:ns AND table_name=:name'),args).one()
                if existing[0]!=args['loc'] or existing[1] not in (None,'TABLE'):
                    raise ValueError('restore would replace an existing Iceberg table')

    def close(self):self.engine.dispose()


def catalog(db,source,store):
    binding=managed(db,source)
    if not binding:return RestCatalog(source,store)
    from .deps import get_orchestrator
    spec,namespace=binding
    password=get_orchestrator().read_layer_secret(namespace,spec.postgres.credentials_secret,'engine-password')
    if not password:raise ValueError('managed Iceberg catalog credentials unavailable')
    query={'connect_timeout':'10','options':'-c statement_timeout=30000'}
    if spec.postgres.tls.mode!='disabled':
        query['sslmode']='verify-full'
        if os.environ.get('SSL_CERT_FILE'):query['sslrootcert']=os.environ['SSL_CERT_FILE']
    url=URL.create('postgresql+psycopg',username='fdp_engine',password=password,
        host='fdp-postgres.'+namespace+'.svc',port=5432,database='iceberg',query=query)
    return JdbcCatalog(url,spec.iceberg.catalog)


class Export:
    def __init__(self,client,warehouse,root,max_bytes,max_objects):
        self.client,self.warehouse,self.root=client,warehouse,root
        self.max_bytes,self.max_objects=max_bytes,max_objects
        self.files={};self.objects={};self.total=0;self.deadline=time.monotonic()+7100

    def copy(self,uri):
        bucket,key=location(uri,self.warehouse);identity=(bucket,key)
        if identity in self.objects:return self.files[self.objects[identity]['member']]
        if time.monotonic()>self.deadline:raise TimeoutError('backup window exceeded')
        if len(self.objects)>=self.max_objects:raise ValueError('backup exceeds configured object limit')
        head=self.client.head_object(Bucket=bucket,Key=key)
        if self.total+head['ContentLength']>self.max_bytes:raise ValueError('backup exceeds configured size limit')
        response=self.client.get_object(Bucket=bucket,Key=key,IfMatch=head['ETag'])
        member='objects/'+hashlib.sha256((bucket+'/'+key).encode()).hexdigest()
        path=self.root/member;path.parent.mkdir(exist_ok=True);digest=hashlib.sha256();size=0
        try:
            with path.open('xb') as stream:
                for chunk in response['Body'].iter_chunks(chunk_size=1024*1024):
                    self.total+=len(chunk);size+=len(chunk)
                    if self.total>self.max_bytes:raise ValueError('backup exceeds configured size limit')
                    if time.monotonic()>self.deadline:raise TimeoutError('backup window exceeded')
                    stream.write(chunk);digest.update(chunk)
        finally:response['Body'].close()
        if size!=head['ContentLength']:raise ValueError('Iceberg object changed during export')
        self.files[member]=path
        self.objects[identity]={'uri':uri,'bucket':bucket,'key':key,'member':member,'size_bytes':size,
            'sha256':digest.hexdigest(),'version_id':response.get('VersionId'),'etag':head['ETag'],
            'content_type':response.get('ContentType','application/octet-stream')}
        return path

    def records(self,uri):
        from fastavro import reader
        with self.copy(uri).open('rb') as stream:
            for index,row in enumerate(reader(stream)):
                if index>=self.max_objects or time.monotonic()>self.deadline:raise ValueError('manifest exceeds configured limits')
                yield row

    def table(self,table):
        path=self.copy(table['metadata_location'])
        with path.open('rb') as raw:
            compressed=raw.read(2)==b'\x1f\x8b';raw.seek(0)
            stream=gzip.GzipFile(fileobj=raw) if compressed else raw
            data=stream.read(64*1024*1024+1)
        if len(data)>64*1024*1024:raise ValueError('table metadata exceeds limit')
        metadata=json.loads(data)
        if metadata.get('format-version') not in (1,2):raise ValueError('Iceberg backup supports table format v1/v2')
        location(metadata['location'].rstrip('/')+'/scope-check',self.warehouse)
        snapshots=metadata.get('snapshots',[])
        snapshot_ids={s['snapshot-id'] for s in snapshots}
        if len(snapshot_ids)!=len(snapshots) or (metadata.get('current-snapshot-id') not in (None,-1) and metadata['current-snapshot-id'] not in snapshot_ids):
            raise ValueError('inconsistent Iceberg snapshot inventory')
        if any(ref['snapshot-id'] not in snapshot_ids for ref in metadata.get('refs',{}).values()):
            raise ValueError('Iceberg branch/tag references a missing snapshot')
        if len(snapshots)>self.max_objects:raise ValueError('snapshot inventory exceeds configured limit')
        for snapshot in snapshots:
            if snapshot.get('manifest-list'):
                manifests=[m['manifest_path'] for m in self.records(snapshot['manifest-list'])]
            elif metadata['format-version']==1:manifests=snapshot.get('manifests',[])
            else:raise ValueError('snapshot has no manifest list')
            for manifest in manifests:
                for entry in self.records(manifest):
                    if entry['status'] not in (0,1,2):raise ValueError('unknown Iceberg manifest entry status')
                    if entry['status']!=2:self.copy(entry['data_file']['file_path'])
        for field,path_key in (('statistics','statistics-path'),('partition-statistics','statistics-path')):
            for stats in metadata.get(field,[]):self.copy(stats[path_key])
        return {**table,'table_uuid':metadata['table-uuid'],'format_version':metadata['format-version'],
            'current_snapshot_id':metadata.get('current-snapshot-id'), 'snapshot_ids':[s['snapshot-id'] for s in snapshots]}


def export(db,frozen,store,root):
    from .backup_adapters import current_source
    source=current_source(db,frozen)
    client=object_client(db,source,store)
    capture=Export(client,source.config['warehouse'],root,frozen['max_bytes'],frozen['max_objects'])
    directory=catalog(db,source,store)
    try:tables,namespaces=directory.tables(frozen['schemas'],frozen['max_objects'])
    finally:directory.close()
    if any(not t['namespace'] or not t['name'] or any(not p or any(c in p for c in '\x00\x1f') for p in t['namespace']) for t in tables):
        raise ValueError('invalid catalog table identifier')
    captured=[capture.table(table) for table in tables]
    return capture.files,{'method':'iceberg-snapshot-files','warehouse':source.config['warehouse'],
        'tables':captured,'namespaces':namespaces,'objects':list(capture.objects.values()),
        'consistency':'Captured metadata and all retained snapshot dependencies per table; no cross-table transaction.',
        'historical_metadata_log':'Older metadata JSON versions and expired snapshots are not included.'}
