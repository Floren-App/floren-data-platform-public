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


def unpack(archive, root):
    with tarfile.open(archive, 'r:') as tar:
        members = tar.getmembers()
        expected = {'metadata.dump','manifest.json','platform-keys.json'}
        if len(members) != 3 or {m.name for m in members} != expected:
            raise ValueError('archive has unexpected or duplicate recovery members')
        for member in members:
            if not member.isfile() or member.size < 0:
                raise ValueError('recovery archive contains a non-file member')
            if member.name != 'metadata.dump' and member.size > 1024*1024:
                raise ValueError('recovery manifest or key file exceeds its bound')
            with tar.extractfile(member) as source, (root/member.name).open('xb') as output:
                os.chmod(root/member.name, 0o600)
                import shutil
                shutil.copyfileobj(source, output, length=envelope.CHUNK)
    manifest = json.loads((root/'manifest.json').read_text())
    if manifest.get('schema') != 'fdp.recovery/1' or envelope.checksum(root/'metadata.dump') != manifest.get('metadata_sha256'):
        raise ValueError('unsupported or inconsistent recovery manifest')
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


def write_keys(root, keys_output):
    # Create atomically with restrictive permissions, including when umask is permissive.
    descriptor = os.open(keys_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        output.write((root/'platform-keys.json').read_text())


def restore(root, target_url, keys_output, archive_sha256=''):
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
        if Path(keys_output).exists():
            raise ValueError('recovery key output already exists; refusing to overwrite')
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
        write_keys(root, keys_output)
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
    parser.add_argument('--private-key-file', type=Path)
    parser.add_argument('--expected-sha256', help='Checksum from trusted backup inventory or external audit evidence')
    parser.add_argument('--private-key-password-env', default='FDP_RECOVERY_KEY_PASSWORD')
    parser.add_argument('--target-url-env', default='FDP_RESTORE_DATABASE_URL')
    parser.add_argument('--keys-output', type=Path)
    parser.add_argument('--confirm-empty-target', action='store_true')
    parser.add_argument('--confirm-restored-target', action='store_true')
    args = parser.parse_args()
    if args.operation != 'quarantine' and not (args.archive and args.private_key_file and args.expected_sha256):
        parser.error('archive operations require --archive, --private-key-file and --expected-sha256')
    if args.operation == 'extract-keys' and not args.keys_output:
        parser.error('extract-keys requires --keys-output')
    if args.operation == 'quarantine' and not args.confirm_restored_target:
        parser.error('quarantine requires --confirm-restored-target')
    if args.operation == 'restore' and (not args.confirm_empty_target or not args.keys_output):
        parser.error('restore requires --confirm-empty-target and --keys-output')
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
            password = os.environ.get(args.private_key_password_env)
            envelope.decrypt(args.archive, root/'recovery.tar', args.private_key_file.read_bytes(),
                args.expected_sha256, password=password.encode() if password else None)
            manifest = unpack(root/'recovery.tar', root)
            if args.operation == 'extract-keys':
                write_keys(root, args.keys_output)
            if args.operation == 'restore':
                target = os.environ.get(args.target_url_env)
                if not target:
                    parser.error('target database environment variable is not set')
                restore(root, target, args.keys_output, args.expected_sha256)
            print(json.dumps({'operation':args.operation, 'verified':True,
                'created_at':manifest['created_at'], 'recovery_quarantine':args.operation == 'restore',
                'external_dependencies':manifest['customer_backups_required']}))
    except Exception as exc:
        # Authentication, database and provider errors may include credentials.
        parser.exit(1, 'Recovery failed (' + type(exc).__name__ + '). No customer data systems were contacted.\n')


if __name__ == '__main__':
    main()
