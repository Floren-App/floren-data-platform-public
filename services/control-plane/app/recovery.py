"""Offline inspection/restore. Never contacts customer data systems or restarts work."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect

from . import backups, recovery_archive as envelope
from .models import PlatformSetting, Run, Workload, Workspace, SqlQuery, PlatformBackup, AuditEvent, SiemCursor, KafkaSession, utcnow


def unpack(archive, root, store=None):
    with tarfile.open(archive, 'r:') as tar:
        members = tar.getmembers()
        expected = {'metadata.dump','manifest.json'}
        names={m.name for m in members}
        legacy = names == expected | {'platform-keys.json'} and len(members)==3
        if not legacy and (len(members) != 2 or names != expected):
            raise ValueError('archive has unexpected or duplicate recovery members')
        for member in members:
            if not member.isfile() or member.size < 0:
                raise ValueError('recovery archive contains a non-file member')
            if member.name != 'metadata.dump' and member.size > 1024*1024:
                raise ValueError('recovery manifest or key file exceeds its bound')
            if member.name == 'platform-keys.json':
                continue  # Legacy secret values are imported into the selected vault, never extracted.
            with tar.extractfile(member) as source, (root/member.name).open('xb') as output:
                os.chmod(root/member.name, 0o600)
                import shutil
                shutil.copyfileobj(source, output, length=envelope.CHUNK)
    manifest = json.loads((root/'manifest.json').read_text())
    if manifest.get('schema') not in ('fdp.recovery/1','fdp.recovery/2') or envelope.checksum(root/'metadata.dump') != manifest.get('metadata_sha256'):
        raise ValueError('unsupported or inconsistent recovery manifest')
    if legacy:
        if manifest.get('schema')!='fdp.recovery/1':
            raise ValueError('unexpected key material in a new-format archive')
        if store is not None:
            with tarfile.open(archive,'r:') as tar:
                keys=json.load(tar.extractfile('platform-keys.json'))
            if not keys or set(keys)-set(backups.KEY_FIELDS) or any(not isinstance(v,str) for v in keys.values()):
                raise ValueError('unsupported legacy key material')
            from .models import new_id
            reference='platform/recovery/runtime/'+new_id()
            store.put(reference,keys)
            manifest={**manifest,'schema':'fdp.recovery/2','platform_keys_ref':reference}
            (root/'manifest.json').write_text(json.dumps(manifest))
    return manifest


def quarantine_connection(connection, archive_sha256=""):
    """Invalidate restored runtime admissions and start a distinct recovery audit stream."""
    # Restored admissions and runtime state must never replay automatically.
    connection.execute(Run.__table__.update().where(Run.status.in_(['pending','queued','running'])).values(status='stopped', finished_at=utcnow()))
    connection.execute(Workload.__table__.update().values(status='stopped', queued_sha=None, queued_at=None, queued_demand=None))
    connection.execute(Workspace.__table__.update().values(status='stopped'))
    connection.execute(SqlQuery.__table__.update().values(expires_at=utcnow()))
    connection.execute(KafkaSession.__table__.update().values(expires_at=0))
    connection.execute(PlatformBackup.__table__.update().where(PlatformBackup.status.in_(['queued','running'])).values(status='failed', error='interrupted at recovery snapshot', finished_at=utcnow()))
    import uuid
    epoch = uuid.uuid4().hex
    connection.execute(PlatformSetting.__table__.delete().where(PlatformSetting.key == backups.RECOVERY_EPOCH))
    connection.execute(PlatformSetting.__table__.insert().values(key=backups.RECOVERY_EPOCH, value={'id':epoch}))
    connection.execute(SiemCursor.__table__.update().values(seq=0, last_hash='0'*64))
    connection.execute(AuditEvent.__table__.insert().values(actor='operator:offline-recovery',action='backup.recovery.restored',target='set-backups',detail={'recovery_epoch':epoch,'archive_sha256':archive_sha256}))
    connection.execute(PlatformSetting.__table__.delete().where(PlatformSetting.key == backups.QUARANTINE))
    connection.execute(PlatformSetting.__table__.insert().values(key=backups.QUARANTINE,
        value={'blocked':True, 'reason':'Restored metadata requires identity, access and dependency reconciliation'}))


def restore_vault_keys(root, store):
    """Reconcile runtime keys inside the chosen vault; never export a secret file."""
    manifest = json.loads((root/'manifest.json').read_text())
    reference = manifest.get('platform_keys_ref', '')
    if not reference.startswith('platform/recovery/runtime/') or len(reference.split('/')) != 4:
        raise ValueError('invalid platform key reference')
    values = store.read(reference)
    if not values or set(values)-set(backups.KEY_FIELDS):
        raise ValueError('recover the referenced platform keys in the configured vault first')
    from .config import Settings
    store.put(Settings().runtime_keys_ref or manifest.get('runtime_keys_ref') or 'platform/runtime', values)



def restore(root, target_url, store, archive_sha256=''):
    backups.pg_environment(target_url)  # Reject non-PostgreSQL before creating any connection.
    engine = create_engine(target_url)
    try:
        with engine.connect() as connection:
            # Reject unrelated tables in any non-system schema, not just public.
            from sqlalchemy import text
            occupied = connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','f','S'))"))
            if occupied:
                raise ValueError('restore target must be an empty, dedicated PostgreSQL database')
        # Verify key availability before mutating the empty recovery target.
        manifest = json.loads((root/'manifest.json').read_text())
        if not store.read(manifest.get('platform_keys_ref','')):
            raise ValueError('referenced platform keys must be recovered in the selected vault first')
        tool = backups.pg_tool('pg_restore', target_url)
        # A committed independent guard fails closed if restore or finalization crashes.
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE fdp_recovery_guard (blocked BOOLEAN NOT NULL)'))
            connection.execute(text('INSERT INTO fdp_recovery_guard VALUES (TRUE)'))
        result = subprocess.run([tool, '--single-transaction', '--exit-on-error', '--no-owner',
            '--no-privileges', '--dbname', backups.make_url(target_url).database, str(root/'metadata.dump')],
            env=backups.pg_environment(target_url), stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=1800)
        if result.returncode:
            raise RuntimeError('restore failed; the transaction was rolled back')
        with engine.begin() as connection:
            quarantine_connection(connection, archive_sha256)
            connection.execute(text('DROP TABLE fdp_recovery_guard'))
        restore_vault_keys(root, store)
    finally:
        engine.dispose()


def quarantine_native_restore(target_url):
    """Call offline on a customer-restored metadata database before starting FDP."""
    backups.pg_environment(target_url)
    engine = create_engine(target_url)
    try:
        with engine.begin() as connection:
            from .schema_migrations import current, head
            if current(connection) != head():
                raise ValueError('upgrade the restored metadata offline before quarantining it')
            quarantine_connection(connection)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['verify','restore','quarantine','extract-keys'])
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--recovery-key-ref', help='Key path in the configured secret backend; fields private_key and optional password')
    parser.add_argument('--expected-sha256', help='Checksum from trusted backup inventory or external audit evidence')
    parser.add_argument('--target-url-env', default='FDP_RESTORE_DATABASE_URL')
    parser.add_argument('--confirm-empty-target', action='store_true')
    parser.add_argument('--confirm-restored-target', action='store_true')
    args = parser.parse_args()
    if args.operation != 'quarantine' and not (args.archive and args.recovery_key_ref and args.expected_sha256):
        parser.error('archive operations require --archive, --recovery-key-ref and --expected-sha256')
    if args.operation == 'quarantine' and not args.confirm_restored_target:
        parser.error('quarantine requires --confirm-restored-target')
    if args.operation == 'restore' and not args.confirm_empty_target:
        parser.error('restore requires --confirm-empty-target')
    try:
        if args.operation == 'quarantine':
            target = os.environ.get(args.target_url_env)
            if not target:
                parser.error('target database environment variable is not set')
            quarantine_native_restore(target)
            print(json.dumps({'operation':'quarantine', 'recovery_quarantine':True}))
            return
        with tempfile.TemporaryDirectory(prefix='fdp-offline-recovery-') as temp:
            root = Path(temp); os.chmod(root, 0o700)
            from .secrets import make_secret_store
            from .config import Settings
            store = make_secret_store(Settings())
            key = store.read(args.recovery_key_ref)
            password = key.get('password')
            envelope.decrypt(args.archive, root/'recovery.tar', key['private_key'].encode(),
                args.expected_sha256, password=password.encode() if password else None)
            manifest = unpack(root/'recovery.tar', root, store if args.operation in ('restore','extract-keys') else None)
            if args.operation == 'extract-keys':
                restore_vault_keys(root, store)
            if args.operation == 'restore':
                target = os.environ.get(args.target_url_env)
                if not target:
                    parser.error('target database environment variable is not set')
                restore(root, target, store, args.expected_sha256)
            print(json.dumps({'operation':args.operation, 'verified':True,
                'created_at':manifest['created_at'], 'recovery_quarantine':args.operation == 'restore',
                'external_dependencies':manifest['customer_backups_required']}))
    except Exception as exc:
        # Authentication, database and provider errors may include credentials.
        parser.exit(1, 'Recovery failed (' + type(exc).__name__ + '). No customer data systems were contacted.\n')


if __name__ == '__main__':
    main()
