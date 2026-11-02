import io
import json
import tarfile
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app import backups, recovery_archive as envelope
from app.db import SessionLocal
from app.models import PlatformBackup, PlatformSetting
from app.recovery import unpack
from .conftest import as_


@pytest.fixture(scope='module')
def recovery_keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return (private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()),
            private.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo).decode())


def config(key):
    return {'bucket':'enterprise-recovery', 'recovery_public_key':key, 'enabled':True}


def test_envelope_round_trip_and_wrong_checksum(tmp_path, recovery_keys):
    private, public = recovery_keys
    plain = tmp_path/'input'; plain.write_bytes(b'platform state\0'*200000)
    encrypted = tmp_path/'ciphertext'
    envelope.encrypt(plain, encrypted, public)
    assert b'platform state' not in encrypted.read_bytes()
    with pytest.raises(ValueError, match='checksum'):
        envelope.decrypt(encrypted, tmp_path/'bad', private, '0'*64)
    restored = tmp_path/'restored'
    envelope.decrypt(encrypted, restored, private, envelope.checksum(encrypted))
    assert restored.read_bytes() == plain.read_bytes()
    assert restored.stat().st_mode & 0o777 == 0o600


def test_tamper_never_leaves_usable_plaintext(tmp_path, recovery_keys):
    private, public = recovery_keys
    plain = tmp_path/'input'; plain.write_bytes(b'sensitive state'*500)
    encrypted = tmp_path/'ciphertext'; envelope.encrypt(plain, encrypted, public)
    data = bytearray(encrypted.read_bytes()); data[-20] ^= 1; encrypted.write_bytes(data)
    output = tmp_path/'plain'
    with pytest.raises(InvalidTag):
        envelope.decrypt(encrypted, output, private, envelope.checksum(encrypted))
    assert not output.exists()


@pytest.mark.parametrize('change', [
    {'endpoint_url':'http://s3.internal'}, {'endpoint_url':'https://user:password@s3.example.com'},
    {'prefix':'../outside'}, {'prefix':'/absolute'}, {'server_side_encryption':'none'},
    {'enabled':True}, {'kms_key_id':'key'},
])
def test_unsafe_or_incomplete_destination_rejected(change):
    with pytest.raises(ValueError):
        backups.BackupConfig.model_validate(change)


def test_backup_api_requires_explicit_permission_and_protects_destination(client, recovery_keys):
    _, public = recovery_keys
    assert client.get('/v1/backups', headers=as_('ana')).status_code == 403
    assert client.put('/v1/backups/configuration', headers=as_('ana'), json=config(public)).status_code == 403
    response = client.put('/v1/backups/configuration', headers=as_('admin'), json=config(public))
    assert response.status_code == 200, response.text
    queued = client.post('/v1/backups', headers=as_('admin'))
    assert queued.status_code == 202, queued.text
    assert 'destination' not in queued.json()
    assert client.post('/v1/backups', headers=as_('admin')).status_code == 409
    with SessionLocal() as db:
        frozen = dict(db.get(PlatformBackup, queued.json()['id']).destination)
    changed = {**config(public), 'bucket':'another-recovery'}
    assert client.put('/v1/backups/configuration', headers=as_('admin'), json=changed).status_code == 200
    with SessionLocal() as db:
        assert db.get(PlatformBackup, queued.json()['id']).destination == frozen


def test_success_requires_s3_readback_and_can_verify_offline(client, recovery_keys, monkeypatch, tmp_path):
    private, public = recovery_keys
    client.put('/v1/backups/configuration', headers=as_('admin'), json=config(public)).raise_for_status()
    response = client.post('/v1/backups', headers=as_('admin')); response.raise_for_status()
    objects = {}
    class Body(io.BytesIO):
        def iter_chunks(self, chunk_size):
            return iter(lambda: self.read(chunk_size), b'')
    class S3:
        def upload_file(self, path, bucket, key, ExtraArgs):
            objects[(bucket,key)] = Path(path).read_bytes()
        def get_object(self, Bucket, Key):
            return {'Body':Body(objects[(Bucket,Key)])}
    monkeypatch.setattr(backups, 'client', lambda *a:S3())
    monkeypatch.setattr(backups, 'dump_metadata', lambda url,path:path.write_bytes(b'PGDMP-test-metadata'))
    backups.run_one(SessionLocal, object())
    with SessionLocal() as db:
        row = db.get(PlatformBackup, response.json()['id'])
        assert row.status == 'succeeded', row.error
        archive = tmp_path/'recovery.fdp'; archive.write_bytes(objects[(row.bucket,row.object_key)])
        tar = tmp_path/'recovery.tar'
        envelope.decrypt(archive, tar, private, row.sha256)
        manifest = unpack(tar, tmp_path)
        assert manifest['database'] == 'platform metadata only'
        assert (tmp_path/'metadata.dump').read_bytes() == b'PGDMP-test-metadata'
        assert 'session_signing_key' in json.loads((tmp_path/'platform-keys.json').read_text())


def test_failed_backup_does_not_report_success_or_leak_credentials(client, recovery_keys, monkeypatch):
    _, public = recovery_keys
    client.put('/v1/backups/configuration', headers=as_('admin'), json=config(public)).raise_for_status()
    row = client.post('/v1/backups', headers=as_('admin')).json()
    def fail(*args):
        raise RuntimeError('password=should-never-appear')
    monkeypatch.setattr(backups, 'dump_metadata', fail)
    backups.run_one(SessionLocal, object())
    result = client.get('/v1/backups', headers=as_('admin')).json()[0]
    assert result['id'] == row['id'] and result['status'] == 'failed'
    assert 'should-never-appear' not in json.dumps(result)
    assert not result['sha256']


def test_archive_rejects_paths_and_links(tmp_path):
    tar = tmp_path/'bad.tar'
    with tarfile.open(tar, 'w') as archive:
        for name in ['../metadata.dump','manifest.json','platform-keys.json']:
            item = tarfile.TarInfo(name); item.size = 2
            archive.addfile(item, io.BytesIO(b'{}'))
    with pytest.raises(ValueError, match='unexpected'):
        unpack(tar, tmp_path)


def test_recovery_quarantine_blocks_actions_until_restart(client):
    from app.main import app
    app.state.recovery_quarantine = True
    try:
        assert client.post('/v1/backups', headers=as_('admin')).status_code == 503
        assert client.get('/v1/backups/recovery', headers=as_('admin')).status_code == 200
        assert client.get('/healthz').status_code == 200
        assert client.get('/v1/backups/recovery', headers=as_('ana')).status_code == 403
    finally:
        app.state.recovery_quarantine = False


def test_key_extraction_is_create_only_and_private(tmp_path):
    from app.recovery import write_keys
    root = tmp_path/'archive'; root.mkdir()
    (root/'platform-keys.json').write_text('{"secret":"recovered"}')
    output = tmp_path/'keys.json'
    write_keys(root, output)
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_keys(root, output)
    assert output.read_text() == '{"secret":"recovered"}'


def test_libpq_keeps_customer_tls_requirements_without_inheriting_other_target(monkeypatch):
    monkeypatch.setenv('PGSSLMODE', 'verify-full')
    monkeypatch.setenv('PGSSLROOTCERT', '/customer/ca.pem')
    monkeypatch.setenv('PGHOST', 'wrong-target')
    monkeypatch.setenv('PGOPTIONS', '-c search_path=untrusted')
    env = backups.pg_environment('postgresql://operator:secret@restore.local/metadata')
    assert env['PGSSLMODE'] == 'verify-full'
    assert env['PGSSLROOTCERT'] == '/customer/ca.pem'
    assert env['PGHOST'] == 'restore.local'
    assert 'PGOPTIONS' not in env
    assert backups.pg_environment('postgresql://restore.local/metadata?sslmode=require')['PGSSLMODE'] == 'require'


def test_native_restore_quarantine_invalidates_admissions_and_starts_new_audit_epoch(client):
    from app.db import engine
    from app.recovery import quarantine_connection
    from app.models import AuditEvent
    with engine.begin() as connection:
        quarantine_connection(connection)
    with SessionLocal() as db:
        assert backups.recovery_blocked(db)
        first = db.get(PlatformSetting, backups.RECOVERY_EPOCH).value['id']
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == 'backup.recovery.restored'))
        assert event.detail['recovery_epoch'] == first
    with engine.begin() as connection:
        quarantine_connection(connection)
    with SessionLocal() as db:
        assert db.get(PlatformSetting, backups.RECOVERY_EPOCH).value['id'] != first


def test_existing_s3_integration_freezes_destination_and_narrows_assumed_role(client, recovery_keys, monkeypatch):
    from app.models import DataSource
    from .rbac_helpers import integration
    import boto3
    response = client.put('/v1/admin/datasources/recovery-store', headers=as_('admin'),
        json=integration('recovery-store','s3',prefix='platform',endpoint_url='https://s3.example.com'))
    response.raise_for_status()
    with SessionLocal() as db:
        source = db.scalar(select(DataSource).where(DataSource.name == 'recovery-store'))
        dest = backups.destination(db, backups.BackupConfig(enabled=True,source_id=source.id,
            recovery_public_key=recovery_keys[1],prefix='archives'))
        source.config = {**source.config, 'bucket':'redirected'}
        db.commit()
    assert dest['bucket'] == 'recovery-store' and dest['prefix'] == 'platform/archives'
    calls = []
    class STS:
        def assume_role(self, **kwargs):
            calls.append(kwargs)
            return {'Credentials':{'AccessKeyId':'session','SecretAccessKey':'short-lived','SessionToken':'token'}}
    class Store:
        def read(self, path):
            assert path == 'datasources/recovery-store/provisioner'
            return {'access_key_id':'provisioner', 'secret_access_key':'secret'}
    def make_client(service, **kwargs):
        if service == 'sts': return STS()
        assert kwargs['aws_access_key_id'] == 'session'
        assert kwargs['aws_session_token'] == 'token'
        return 'scoped-s3'
    monkeypatch.setattr(boto3, 'client', make_client)
    assert backups.client(dest,Store()) == 'scoped-s3'
    policy = json.loads(calls[0]['Policy'])
    assert policy['Statement'][0]['Resource'] == ['arn:aws:s3:::recovery-store/platform/archives/*']
    assert 's3:DeleteObject' not in policy['Statement'][0]['Action']
    assert 's3:ListBucket' not in policy['Statement'][0]['Action']
