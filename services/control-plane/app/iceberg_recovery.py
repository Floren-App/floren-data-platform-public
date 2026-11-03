"""Offline Iceberg recovery to original warehouse paths and an empty catalog.

Use storage_recovery first to decrypt and verify an archive into a new directory.
This explicit recovery command writes storage and registers tables; it never
replaces a conflicting object or catalog pointer. Credentials use the existing vault.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

from . import iceberg_backup as iceberg
from . import recovery_archive


def code(exc):
    return getattr(exc,'response',{}).get('Error',{}).get('Code','')


def verify_existing(client,bucket,key,expected):
    try:response=client.get_object(Bucket=bucket,Key=key)
    except Exception as exc:
        if code(exc) in ('NoSuchKey','404','NotFound'):return False
        raise
    digest=hashlib.sha256();size=0
    try:
        for chunk in response['Body'].iter_chunks(chunk_size=1024*1024):
            size+=len(chunk)
            if size>expected['size_bytes']:raise ValueError('existing recovery object conflicts with backup')
            digest.update(chunk)
    finally:response['Body'].close()
    if size!=expected['size_bytes'] or digest.hexdigest()!=expected['sha256']:
        raise ValueError('existing recovery object conflicts with backup')
    return True


def upload_new(client,path,obj):
    """Conditional final publication for small and multipart objects, never overwrite."""
    bucket,key=obj['bucket'],obj['key'];size=path.stat().st_size
    if verify_existing(client,bucket,key,obj):return
    if size<=64*1024*1024:
        try:
            with path.open('rb') as stream:
                client.put_object(Bucket=bucket,Key=key,Body=stream,ContentLength=size,
                    ContentType=obj.get('content_type','application/octet-stream'),IfNoneMatch='*')
        except Exception as exc:
            if code(exc) not in ('PreconditionFailed','412','ConditionalRequestConflict','409'):raise
            if not verify_existing(client,bucket,key,obj):raise ValueError('concurrent recovery object conflict') from None
    else:
        upload=client.create_multipart_upload(Bucket=bucket,Key=key,ContentType=obj.get('content_type','application/octet-stream'))['UploadId']
        try:
            parts=[];chunk_size=max(8*1024*1024,math.ceil(size/9999))
            with path.open('rb') as stream:
                while True:
                    chunk=stream.read(chunk_size)
                    if not chunk:break
                    number=len(parts)+1
                    part=client.upload_part(Bucket=bucket,Key=key,UploadId=upload,PartNumber=number,Body=chunk)
                    parts.append({'PartNumber':number,'ETag':part['ETag']})
            client.complete_multipart_upload(Bucket=bucket,Key=key,UploadId=upload,
                MultipartUpload={'Parts':parts},IfNoneMatch='*')
        except Exception as exc:
            try:client.abort_multipart_upload(Bucket=bucket,Key=key,UploadId=upload)
            except Exception:pass  # Best-effort cleanup must not hide the publication failure.
            if code(exc) not in ('PreconditionFailed','412','ConditionalRequestConflict','409'):raise
            if not verify_existing(client,bucket,key,obj):raise ValueError('concurrent recovery object conflict') from None
    if not verify_existing(client,bucket,key,obj):raise ValueError('restored object missing after upload')


def validate(root,manifest):
    """Verify payload hashes and dependency closure before contacting a recovery target."""
    if manifest.get('schema')!='fdp.storage-backup/1' or manifest.get('source_kind')!='iceberg' or manifest.get('method')!='iceberg-snapshot-files':
        raise ValueError('not a supported Iceberg backup')
    warehouse=manifest['warehouse'];objects={};tables=set()
    import re
    for obj in manifest['objects']:
        identity=iceberg.location(obj['uri'],warehouse)
        if identity!=(obj['bucket'],obj['key']) or identity in objects:raise ValueError('duplicate or inconsistent object identity')
        member=obj['member']
        if not re.fullmatch('objects/[0-9a-f]{64}',member):raise ValueError('invalid archive member')
        path=root/member
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size!=obj['size_bytes'] or recovery_archive.checksum(path)!=obj['sha256']:
            raise ValueError('recovery payload verification failed')
        objects[identity]=obj
    class VerifyGraph(iceberg.Export):
        def copy(self,uri):
            identity=iceberg.location(uri,self.warehouse)
            obj=objects[identity]  # A missing referenced file fails before target access.
            self.objects[identity]=obj
            return root/obj['member']
    captured=VerifyGraph(None,warehouse,root,sum(o['size_bytes'] for o in objects.values()),max(1,len(objects)))
    for table in manifest['tables']:
        ns=table['namespace'];name=table['name']
        if not ns or any(not isinstance(p,str) or not p or any(c in p for c in '\x00\x1f') for p in ns) or not isinstance(name,str) or not name or any(c in name for c in '\x00\x1f'):
            raise ValueError('invalid recovery table identifier')
        identity=(tuple(ns),name)
        if identity in tables:raise ValueError('duplicate recovery table')
        tables.add(identity)
        checked=captured.table(table)
        if any(checked[k]!=table[k] for k in ('table_uuid','format_version','current_snapshot_id','snapshot_ids')):
            raise ValueError('catalog snapshot does not match metadata')
    if set(captured.objects)!=set(objects):raise ValueError('archive contains objects outside the captured dependency graph')
    for ns in manifest['namespaces']:
        if not ns or any(not isinstance(p,str) or not p or any(c in p for c in '\x00\x1f') for p in ns):raise ValueError('invalid recovery namespace')


def restore(db,source,store,root):
    manifest=json.loads((root/'manifest.json').read_text())
    validate(root,manifest)
    if source.kind!='iceberg' or source.config['warehouse'].rstrip('/')!=manifest['warehouse'].rstrip('/'):
        raise ValueError('recovery integration must address the original warehouse URI; relocation is not supported')
    client=iceberg.object_client(db,source,store);directory=iceberg.catalog(db,source,store)
    try:
        directory.check(manifest['tables'])
        # Preflight all existing objects before any write or catalog modification.
        for obj in manifest['objects']:verify_existing(client,obj['bucket'],obj['key'],obj)
        for obj in manifest['objects']:upload_new(client,root/obj['member'],obj)
        namespaces={tuple(n) for n in manifest['namespaces']}
        for table in manifest['tables']:
            ns=tuple(table['namespace']);namespaces.update(ns[:i] for i in range(1,len(ns)+1))
        directory.register(manifest['tables'],[list(n) for n in namespaces])
    finally:directory.close()
    return {'restored_tables':len(manifest['tables']),'verified_objects':len(manifest['objects'])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verified-directory',required=True,type=Path)
    parser.add_argument('--target-source-id',required=True,help='Existing Iceberg integration with original warehouse URI and recovery credentials in the selected vault')
    args=parser.parse_args()
    try:
        from .db import SessionLocal
        from .models import DataSource
        from .deps import get_secret_store
        with SessionLocal() as db:
            source=db.get(DataSource,args.target_source_id)
            if not source:raise ValueError('target integration not found')
            result=restore(db,source,get_secret_store(),args.verified_directory)
        print(json.dumps(result))
    except Exception as exc:
        parser.exit(1,'Iceberg recovery failed ('+type(exc).__name__+'). Conflicting objects/tables are never overwritten.\n')


if __name__=='__main__':main()
