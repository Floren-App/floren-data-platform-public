"""Customer-operated recovery of FDP state; never backs up registered business data."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import tarfile
import tempfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url

from .config import get_settings
from .models import DataSource, PlatformBackup, PlatformSetting, Resource, utcnow
from . import recovery_archive as envelope

SETTING = 'platform.backups'
QUARANTINE = 'platform.recovery_quarantine'
RESTORE_GUARD = 'fdp_recovery_guard'
RECOVERY_EPOCH = 'platform.recovery_epoch'
LOCK = 73410891
log = logging.getLogger(__name__)
KEY_FIELDS = ('session_signing_key', 'session_signing_previous_keys', 'trino_signing_key',
              'trino_jwt_private_key', 'trino_jwt_previous_keys', 'gateway_config_token',
              'egress_proxy_token', 'siem_signing_key')


def incomplete_restore(db):
    return inspect(db.bind).has_table(RESTORE_GUARD)


def recovery_blocked(db):
    if incomplete_restore(db):
        return True
    row = db.get(PlatformSetting, QUARANTINE)
    return bool(row and row.value.get('blocked'))


def recovery_gate(request: Request):
    path = request.url.path
    if path in ('/healthz', '/readyz', '/v1/me', '/internal/metrics') or path.startswith(('/v1/backups/recovery', '/scim/')):
        return
    # A recovered process stays quarantined until restarted, even after approval.
    if getattr(request.app.state, 'recovery_quarantine', False):
        raise HTTPException(503, 'Recovery quarantine: reconcile identities, access and dependencies before resuming')


class BackupConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    bucket: str = Field(default='', max_length=63)
    prefix: str = Field(default='fdp-recovery', min_length=1, max_length=200)
    endpoint_url: str = Field(default='', max_length=2048)
    region: str = Field(default='us-east-1', min_length=1, max_length=64)
    source_id: str = Field(default='', max_length=12)
    interval_hours: int = Field(default=24, ge=1, le=720)
    recovery_public_key: str = Field(default='', max_length=8192)
    server_side_encryption: str = ''  # optional in addition to mandatory client encryption
    kms_key_id: str = Field(default='', max_length=2048)
    object_lock_days: int = Field(default=0, ge=0, le=36500)

    @model_validator(mode='after')
    def validate_destination(self):
        if self.bucket and not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', self.bucket):
            raise ValueError('invalid S3 bucket name')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_-]*', self.prefix) or '//' in self.prefix:
            raise ValueError('use a relative backup prefix without traversal or empty components')
        if self.prefix.endswith('/'):
            raise ValueError('backup prefix must not end with a slash')
        if self.endpoint_url:
            url = urlsplit(self.endpoint_url)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
                raise ValueError('S3 endpoint must be an HTTPS origin without credentials or a path')
        if self.server_side_encryption not in ('', 'AES256', 'aws:kms'):
            raise ValueError('unsupported S3 encryption mode')
        if self.kms_key_id and self.server_side_encryption != 'aws:kms':
            raise ValueError('KMS key requires aws:kms server-side encryption')
        if self.source_id and self.server_side_encryption == 'aws:kms' and not self.kms_key_id.startswith('arn:'):
            raise ValueError('integration-backed SSE-KMS requires an explicit KMS key ARN')
        if self.recovery_public_key:
            envelope.public_key(self.recovery_public_key)
        if self.enabled and (not self.recovery_public_key or not (self.bucket or self.source_id)):
            raise ValueError('enabled backups require a recovery public key and S3 destination')
        return self


def configuration(db):
    setting = db.get(PlatformSetting, SETTING)
    return BackupConfig.model_validate(setting.value if setting else {})


def destination(db, config):
    value = config.model_dump()
    if config.source_id:
        source = db.get(DataSource, config.source_id)
        if not source or source.kind != 's3' or source.config.get('managed_rustfs'):
            raise HTTPException(422, 'backup integration must reference external S3 files storage')
        cfg = source.config
        if config.bucket and config.bucket != cfg.get('bucket'):
            raise HTTPException(422, 'backup bucket must match the referenced integration')
        # Freeze these fields. Editing an integration cannot redirect queued backups.
        value.update(bucket=cfg.get('bucket', ''), endpoint_url=cfg.get('endpoint_url', ''),
                     region=cfg.get('region', 'us-east-1'))
        base = cfg.get('prefix', '').strip('/')
        value['prefix'] = '/'.join(x for x in (base, config.prefix) if x)
        value['credential_path'] = 'datasources/' + source.name + '/provisioner'
        value['role_arn'] = cfg.get('role_arn', '')
        value['sts_endpoint_url'] = cfg.get('sts_endpoint_url', '')
    # Revalidate copied endpoint and prefix as strictly as a direct destination.
    BackupConfig.model_validate({k:v for k,v in value.items() if k in BackupConfig.model_fields})
    if not value['bucket'] or not value['recovery_public_key']:
        raise HTTPException(422, 'configure an S3 bucket and recovery key before creating a backup')
    for name in ('sts_endpoint_url',):
        if value.get(name):
            url = urlsplit(value[name])
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
                raise HTTPException(422, 'backup STS endpoint must be an HTTPS origin')
    return value


def client(dest, store):
    import boto3
    from botocore.config import Config
    auth = {}
    verify = os.environ.get('AWS_CA_BUNDLE') or os.environ.get('SSL_CERT_FILE') or True
    if dest.get('credential_path'):
        credentials = store.read(dest['credential_path'])
        if not credentials.get('access_key_id') or not credentials.get('secret_access_key'):
            raise ValueError('backup integration credentials are unavailable')
        auth = {'aws_access_key_id':credentials['access_key_id'],
                'aws_secret_access_key':credentials['secret_access_key']}
        if credentials.get('session_token'):
            auth['aws_session_token'] = credentials['session_token']
        if not dest.get('role_arn'):
            raise ValueError('backup integration requires its STS role')
        prefix = dest['prefix'] + '/'
        policy = {'Version':'2012-10-17', 'Statement':[{'Effect':'Allow',
            'Action':['s3:PutObject', 's3:GetObject', 's3:AbortMultipartUpload', 's3:PutObjectRetention'],
            'Resource':['arn:aws:s3:::' + dest['bucket'] + '/' + prefix + '*']}]}
        if dest.get('kms_key_id'):
            policy['Statement'].append({'Effect':'Allow', 'Action':['kms:GenerateDataKey','kms:Decrypt'],
                                        'Resource':[dest['kms_key_id']]})
        sts = boto3.client('sts', endpoint_url=dest.get('sts_endpoint_url') or None,
            region_name=dest['region'], verify=verify, **auth, config=Config(connect_timeout=10, read_timeout=30))
        c = sts.assume_role(RoleArn=dest['role_arn'], RoleSessionName='fdp-backup',
            DurationSeconds=3600, Policy=json.dumps(policy))['Credentials']
        auth = dict(aws_access_key_id=c['AccessKeyId'], aws_secret_access_key=c['SecretAccessKey'],
                    aws_session_token=c['SessionToken'])
    # No integration: use the installation's AWS workload identity / standard SDK chain.
    return boto3.client('s3', endpoint_url=dest.get('endpoint_url') or None,
        region_name=dest['region'], verify=verify,
        config=Config(connect_timeout=10, read_timeout=120, retries={'max_attempts':3},
                      s3={'addressing_style':'path'}), **auth)


def pg_environment(database_url):
    url = make_url(database_url)
    if url.get_backend_name() != 'postgresql':
        raise ValueError('platform recovery requires PostgreSQL')
    # libpq credentials in the subprocess environment, never argv or logs.
    env = dict(os.environ)
    tls_environment = {key: value for key, value in env.items()
        if key in ('PGSSLMODE', 'PGSSLROOTCERT', 'PGSSLCERT', 'PGSSLKEY',
                   'PGSSLCRL', 'PGSSLCRLDIR', 'PGSSL_MIN_PROTOCOL_VERSION',
                   'PGSSL_MAX_PROTOCOL_VERSION', 'PGCHANNELBINDING')}
    for key in list(env):
        if key.startswith('PG'):
            del env[key]
    env.update(tls_environment)
    for name, value in [('PGHOST', url.host), ('PGPORT', url.port or 5432),
                        ('PGDATABASE', url.database), ('PGUSER', url.username), ('PGPASSWORD', url.password)]:
        if value is not None:
            env[name] = str(value)
    for key in ('sslmode', 'sslrootcert', 'sslcert', 'sslkey'):
        if key in url.query:
            env['PG' + key.upper()] = str(url.query[key])
    env['PGCONNECT_TIMEOUT'] = '15'
    return env


def pg_tool(name, database_url):
    directory = os.environ.get('FDP_POSTGRES_BIN_DIR', '')
    tool = str(Path(directory)/name) if directory else name
    version = subprocess.run([tool, '--version'], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=10, check=True).stdout.decode()
    match = re.search(r'PostgreSQL\)\s+(\d+)', version)
    from sqlalchemy import create_engine
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            major = int(connection.scalar(text('SHOW server_version_num'))) // 10000
    finally:
        engine.dispose()
    if not match or int(match[1]) != major:
        raise RuntimeError('Recovery requires PostgreSQL client tools matching the database major version')
    return tool


def dump_metadata(database_url, path):
    # pg_dump archives the database. Refuse a shared business database rather than
    # silently including customer tables in an archive described as platform state.
    from sqlalchemy import create_engine
    from .db import Base
    engine = create_engine(database_url)
    try:
        default_schema = inspect(engine).default_schema_name
        expected = {(table.schema or default_schema, table.name) for table in Base.metadata.tables.values()}
        expected.update((default_schema,name) for name in ('alembic_version','fdp_migrations'))
        with engine.connect() as connection:
            relations = connection.execute(text("SELECT n.nspname,c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','v','m','f')"))
            if any((schema,name) not in expected for schema,name in relations
                   if schema != 'information_schema' and not schema.startswith('pg_')):
                raise ValueError('FDP metadata backup requires a dedicated platform database')
    finally:
        engine.dispose()
    result = subprocess.run([pg_tool('pg_dump', database_url), '--format=custom', '--no-owner', '--no-privileges',
        '--file', str(path)], env=pg_environment(database_url), stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, timeout=1800)
    if result.returncode:
        raise RuntimeError('PostgreSQL backup failed; check database permissions and client/server compatibility')


def inventory(db):
    return {'schema':'fdp.recovery/1', 'created_at':utcnow().isoformat(),
        'database':'platform metadata only',
        'dependencies':[{'id':s.id, 'name':s.name, 'kind':s.kind, 'layer_id':s.config.get('layer_id')}
                        for s in db.scalars(select(DataSource))],
        'layers':[{'id':r.id, 'name':r.name} for r in db.scalars(select(Resource).where(Resource.kind == 'layer'))],
        'customer_backups_required':['external data and Iceberg catalog', 'IdP and directory',
            'secret store', 'workspace/artifact/package volumes', 'Kafka and Airflow state',
            'Git and Terraform state'],
        'keys':list(KEY_FIELDS)}


def bundle(db, dump, output, settings):
    manifest = inventory(db)
    manifest['platform_release'] = settings.release
    manifest['metadata_revision'] = db.scalar(text('SELECT version_num FROM alembic_version'))
    manifest['metadata_sha256'] = envelope.checksum(dump)
    # Encrypted recovery material is never returned by an API or stored in the metadata DB.
    keys = {name:getattr(settings, name) for name in KEY_FIELDS if getattr(settings, name, None)}
    with tarfile.open(output, 'w') as archive:
        archive.add(dump, arcname='metadata.dump', recursive=False)
        for name, value in [('manifest.json', manifest), ('platform-keys.json', keys)]:
            data = json.dumps(value, sort_keys=True).encode()
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))


def queue(db, actor):
    # Lock the existing setting row so manual and scheduled admission cannot race.
    db.scalar(select(PlatformSetting).where(PlatformSetting.key == SETTING).with_for_update())
    active = db.scalar(select(PlatformBackup).where(PlatformBackup.status.in_(['queued','running'])))
    if active:
        raise HTTPException(409, 'a platform backup is already queued or running')
    row = PlatformBackup(actor=actor, destination=destination(db, configuration(db)))
    db.add(row); db.flush()
    return row


def run_one(factory, store, settings=None):
    settings = settings or get_settings()
    with factory() as db:
        row = db.scalar(select(PlatformBackup).where(PlatformBackup.status == 'queued')
            .order_by(PlatformBackup.created_at).with_for_update(skip_locked=True))
        if not row:
            return
        ident, dest = row.id, dict(row.destination)
        row.status = 'running'; db.commit()
        try:
            # A second leader cannot run a concurrent dump across failover.
            with db.bind.connect() as lock:
                if lock.dialect.name == 'postgresql' and not lock.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key':LOCK}):
                    row.status = 'queued'; db.commit(); return
                try:
                    with tempfile.TemporaryDirectory(prefix='fdp-recovery-') as temp:
                        root = Path(temp); os.chmod(root, 0o700)
                        dump, tar, encrypted = root/'metadata.dump', root/'recovery.tar', root/'recovery.fdp'
                        dump_metadata(settings.database_url, dump)
                        bundle(db, dump, tar, settings)
                        envelope.encrypt(tar, encrypted, dest['recovery_public_key'])
                        checksum, size = envelope.checksum(encrypted), encrypted.stat().st_size
                        key = dest['prefix'] + '/' + ident + '/metadata.fdp'
                        extra = {'ContentType':'application/octet-stream', 'Metadata':{'sha256':checksum, 'format':'fdp-recovery-1'}}
                        if dest.get('server_side_encryption'):
                            extra['ServerSideEncryption'] = dest['server_side_encryption']
                        if dest.get('kms_key_id'):
                            extra['SSEKMSKeyId'] = dest['kms_key_id']
                        if dest.get('object_lock_days'):
                            extra.update(ObjectLockMode='COMPLIANCE', ObjectLockRetainUntilDate=utcnow()+timedelta(days=dest['object_lock_days']))
                        s3 = client(dest, store)
                        s3.upload_file(str(encrypted), dest['bucket'], key, ExtraArgs=extra)
                        # Read back the encrypted object before reporting success; an ETag isn't a SHA-256.
                        response = s3.get_object(Bucket=dest['bucket'], Key=key)
                        digest, received = hashlib.sha256(), 0
                        try:
                            for chunk in response['Body'].iter_chunks(chunk_size=envelope.CHUNK):
                                digest.update(chunk); received += len(chunk)
                                if received > size:
                                    raise ValueError("S3 read-back exceeds the uploaded archive size")
                        finally:
                            response['Body'].close()
                        if digest.hexdigest() != checksum or received != size:
                            raise ValueError('S3 read-back verification failed')
                        row.status, row.bucket, row.object_key = 'succeeded', dest['bucket'], key
                        row.sha256, row.size_bytes = checksum, size
                finally:
                    if lock.dialect.name == 'postgresql':
                        lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key':LOCK})
        except Exception as exc:
            # Provider/process errors may contain credentials or sensitive metadata.
            row.status, row.error = 'failed', 'backup failed (' + type(exc).__name__ + '); consult the recovery runbook'
            log.warning('platform backup %s failed (%s)', ident, type(exc).__name__)
        row.finished_at = utcnow()
        from .models import AuditEvent
        db.add(AuditEvent(actor='system:backup', action='backup.'+row.status, target=ident,
            detail={'sha256':row.sha256, 'size_bytes':row.size_bytes, 'object_key':row.object_key}))
        db.commit()


def tick(factory, store, settings=None):
    with factory() as db:
        config = configuration(db)
        stale = list(db.scalars(select(PlatformBackup).where(PlatformBackup.status.in_(['queued','running']),
            PlatformBackup.created_at < utcnow()-timedelta(hours=2))))
        for row in stale:
            row.status, row.error, row.finished_at = 'failed', 'backup interrupted or exceeded recovery window', utcnow()
        db.commit()
        latest = db.scalar(select(PlatformBackup).order_by(PlatformBackup.created_at.desc()))
        last = latest.created_at.replace(tzinfo=utcnow().tzinfo) if latest else None
        if config.enabled and (not last or utcnow()-last >= timedelta(hours=config.interval_hours)):
            try:
                queue(db, 'system:backup-scheduler'); db.commit()
            except HTTPException as exc:
                db.rollback()
                if exc.status_code != 409:
                    raise
    run_one(factory, store, settings)


async def backup_loop(factory, store, settings=None):
    while True:
        try:
            await asyncio.to_thread(tick, factory, store, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('backup scheduler unavailable (%s)', type(exc).__name__)
        await asyncio.sleep(30)
