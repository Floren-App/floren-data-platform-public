"""Verify/extract a storage backup offline. Never writes to the original integration."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from . import recovery_archive as envelope


def unpack(archive,root):
    with tarfile.open(archive,'r:') as tar:
        members=tar.getmembers();names=[m.name for m in members]
        if len(set(names))!=len(names) or 'manifest.json' not in names:
            raise ValueError('missing or duplicate archive members')
        entry=tar.getmember('manifest.json')
        if not entry.isfile() or entry.size>64*1024*1024:
            raise ValueError('invalid storage manifest')
        manifest=json.load(tar.extractfile(entry))
        if manifest.get('schema')!='fdp.storage-backup/1':raise ValueError('unsupported storage archive')
        expected={'manifest.json'}
        checks={}
        if manifest.get('source_kind')=='postgres':
            checks['database.dump']=(manifest['database_sha256'],None)
        elif manifest.get('source_kind') in ('s3','iceberg'):
            for obj in manifest['objects']:
                name=obj['member']
                if not re.fullmatch(r'objects/[0-9a-f]{64}',name) or name in checks:
                    raise ValueError('invalid or duplicate stored object')
                checks[name]=(obj['sha256'],obj['size_bytes'])
        else:raise ValueError('unsupported storage kind')
        expected.update(checks)
        if set(names)!=expected:raise ValueError('unexpected storage archive member')
        for member in members:
            if not member.isfile():raise ValueError('non-file archive member')
            target=root/member.name;target.parent.mkdir(parents=True,exist_ok=True)
            descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as out,tar.extractfile(member) as source:
                shutil.copyfileobj(source,out,length=envelope.CHUNK)
            if member.name in checks:
                checksum,size=checks[member.name]
                if envelope.checksum(target)!=checksum or (size is not None and target.stat().st_size!=size):
                    raise ValueError('storage object verification failed')
        return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--expected-sha256',required=True)
    parser.add_argument('--recovery-key-ref',required=True)
    parser.add_argument('--extract-to',type=Path,help='New directory for verified native dump or object payloads; omitted verifies only')
    args=parser.parse_args()
    try:
        from .secrets import make_secret_store
        from .config import Settings
        key=make_secret_store(Settings()).read(args.recovery_key_ref)
        if args.extract_to and args.extract_to.exists():raise ValueError('extraction target already exists')
        with tempfile.TemporaryDirectory(prefix='fdp-storage-verify-') as temp:
            root=Path(temp);os.chmod(root,0o700)
            envelope.decrypt(args.archive,root/'archive.tar',key['private_key'].encode(),args.expected_sha256,
                password=key['password'].encode() if key.get('password') else None)
            payload=root/'payload';payload.mkdir(mode=0o700)
            manifest=unpack(root/'archive.tar',payload)
            if args.extract_to:
                # Never merge with an existing directory or follow user-controlled member names.
                shutil.copytree(payload,args.extract_to,dirs_exist_ok=False)
            print(json.dumps({'verified':True,'source_kind':manifest['source_kind'],'source_id':manifest['source_id']}))
    except Exception as exc:
        parser.exit(1,'Storage recovery failed ('+type(exc).__name__+'). No source storage was changed.\n')


if __name__=='__main__':main()
