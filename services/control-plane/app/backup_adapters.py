"""Native exports. No SQL row export is substituted for a recoverable database backup."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path

from . import backups
from .models import DataSource


def current_source(db, frozen):
    source=db.get(DataSource,frozen['id'])
    if not source or source.name!=frozen['name'] or source.kind!=frozen['kind'] or source.config!=frozen['config']:
        raise ValueError('storage integration changed after this backup was queued')
    return source


def export_postgres(db, frozen, store, root):
    from sqlalchemy.engine import URL
    source=current_source(db,frozen);cfg=source.config
    secret=store.read('datasources/'+source.name+'/provisioner')
    if not secret.get('username') or not secret.get('password'):
        raise ValueError('database backup credentials unavailable')
    url=URL.create('postgresql+psycopg',username=secret['username'],password=secret['password'],
        host=cfg['host'],port=int(cfg.get('port',5432)),database=cfg['database'],
        query={key:str(cfg[key]) for key in ('sslmode','sslrootcert','sslcert','sslkey') if cfg.get(key)})
    uri=url.render_as_string(hide_password=False)
    args=[backups.pg_tool('pg_dump',uri),'--format=custom','--no-owner','--no-privileges']
    for schema in frozen['schemas']:
        args += ['--schema', '"'+schema.replace('"','""')+'"']
    path=root/'database.dump'
    with (root/'postgres.stderr').open('wb') as stderr, path.open('wb') as output:
        process=subprocess.Popen(args,env=backups.pg_environment(uri),stdout=subprocess.PIPE,stderr=stderr)
        watchdog=threading.Timer(7100,process.kill);watchdog.daemon=True;watchdog.start()
        try:
            size=0
            while True:
                chunk=process.stdout.read(1024*1024)
                if not chunk:break
                size+=len(chunk)
                if size>frozen['max_bytes']:raise ValueError('backup exceeds configured size limit')
                output.write(chunk)
            if process.wait(timeout=30):raise RuntimeError('PostgreSQL native backup failed')
        finally:
            watchdog.cancel()
            process.stdout.close()
            if process.poll() is None:process.kill();process.wait(timeout=10)
    return {'database.dump':path}, {'method':'postgresql-pg-dump','schemas':frozen['schemas'],
        'database_sha256':backups.envelope.checksum(path)}


def source_client(db, source, store, prefix):
    import boto3
    from botocore.config import Config
    from .storage_access import credentials
    bucket=source.config['bucket'];object_prefix=prefix+'/' if prefix else ''
    policy={'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['s3:GetBucketLocation'],'Resource':['arn:aws:s3:::'+bucket]},
        {'Effect':'Allow','Action':['s3:ListBucket'],'Resource':['arn:aws:s3:::'+bucket],
         'Condition':{'StringLike':{'s3:prefix':[object_prefix+'*']}}},
        {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],
         'Resource':['arn:aws:s3:::'+bucket+'/'+object_prefix+'*']}]}
    auth,_=credentials(source,{}, {},store,db=db,policy_override=policy)
    return boto3.client('s3',endpoint_url=source.config.get('endpoint_url') or None,
        region_name=source.config.get('region','us-east-1'),verify=os.environ.get('SSL_CERT_FILE') or True,
        aws_access_key_id=auth['access_key_id'],aws_secret_access_key=auth['secret_access_key'],
        aws_session_token=auth['session_token'],config=Config(connect_timeout=10,read_timeout=120,
            s3={'addressing_style':'path'}))


def export_s3(db, frozen, store, root):
    from .storage_access import prefix_path
    source=current_source(db,frozen);cfg=source.config
    prefix=prefix_path(cfg.get('prefix',''),frozen['source_prefix'])
    prefix=prefix+'/' if prefix else ''
    client=source_client(db,source,store,prefix.rstrip('/'))
    files={};objects=[];total=0;deadline=time.monotonic()+7100
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=cfg['bucket'],Prefix=prefix):
        for item in page.get('Contents',[]):
            if time.monotonic()>deadline:raise TimeoutError('backup window exceeded')
            key=item['Key']
            if not key.startswith(prefix):raise ValueError('S3 listing escaped the source prefix')
            if len(objects)>=frozen['max_objects']:raise ValueError('backup exceeds configured object limit')
            response=client.get_object(Bucket=cfg['bucket'],Key=key,IfMatch=item['ETag'])
            # Remote keys never become local paths or tar member names.
            name='objects/'+hashlib.sha256(key.encode()).hexdigest()
            path=root/name;path.parent.mkdir(exist_ok=True);size=0;digest=hashlib.sha256()
            try:
                with path.open('xb') as output:
                    for chunk in response['Body'].iter_chunks(chunk_size=1024*1024):
                        if time.monotonic()>deadline:raise TimeoutError('backup window exceeded')
                        size+=len(chunk);total+=len(chunk)
                        if total>frozen['max_bytes']:raise ValueError('backup exceeds configured size limit')
                        digest.update(chunk);output.write(chunk)
            finally:response['Body'].close()
            if size!=item['Size']:raise ValueError('source object size changed during backup')
            files[name]=path;objects.append({'key':key,'member':name,'size_bytes':size,'sha256':digest.hexdigest(),
                'version_id':response.get('VersionId'),'etag':item['ETag'],
                'content_type':response.get('ContentType','application/octet-stream')})
    return files, {'method':'s3-current-objects','bucket':cfg['bucket'],'prefix':prefix,'objects':objects,
        'consistency':'Per-object conditional reads; no cross-object transaction or historical-version export.'}


def bundle(db, frozen, store, root, output):
    if frozen['kind']=='postgres':files,details=export_postgres(db,frozen,store,root)
    elif frozen['kind']=='s3':files,details=export_s3(db,frozen,store,root)
    elif frozen['kind']=='iceberg':
        from .iceberg_backup import export
        files,details=export(db,frozen,store,root)
    else:raise ValueError('unsupported native backup adapter')
    manifest={'schema':'fdp.storage-backup/1','source_id':frozen['id'],'source_name':frozen['name'],
        'source_kind':frozen['kind'],'created_at':backups.utcnow().isoformat(),**details}
    with tarfile.open(output,'w') as tar:
        for name,path in files.items():tar.add(path,arcname=name,recursive=False)
        data=json.dumps(manifest,sort_keys=True).encode()
        if len(data)>64*1024*1024:raise ValueError('storage manifest exceeds the recovery limit')
        info=tarfile.TarInfo('manifest.json');info.size=len(data);info.mode=0o600
        tar.addfile(info,io.BytesIO(data))
