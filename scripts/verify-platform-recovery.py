#!/usr/bin/env python3
"""Exercise real PostgreSQL migrations, encrypted backup and empty-target recovery.

FDP_RECOVERY_TEST_POSTGRES_URL must name an isolated disposable PostgreSQL server
whose login may create databases. Only two newly generated databases are changed.
No customer source, IdP, secret store, S3 endpoint or Kubernetes object is contacted.
"""
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'services/control-plane'))


def main():
    import psycopg
    from psycopg import sql
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app import backups, recovery_archive as envelope
    from app.config import Settings
    from app.models import PlatformBackup, PlatformSetting
    from app.recovery import unpack, restore, quarantine_native_restore
    from app.schema_migrations import upgrade, require_current

    raw = os.environ.get('FDP_RECOVERY_TEST_POSTGRES_URL')
    if not raw:
        raise SystemExit('Set FDP_RECOVERY_TEST_POSTGRES_URL to an isolated disposable PostgreSQL server')
    url = make_url(raw)
    connection_url = url.set(drivername='postgresql').render_as_string(hide_password=False)
    databases = ['fdp_recovery_source_'+uuid.uuid4().hex[:12], 'fdp_recovery_target_'+uuid.uuid4().hex[:12]]
    engines = []
    with psycopg.connect(connection_url, autocommit=True) as admin:
        try:
            for name in databases:
                admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            urls = [url.set(drivername='postgresql+psycopg', database=name).render_as_string(hide_password=False) for name in databases]
            source, target = [create_engine(u) for u in urls]; engines = [source,target]
            # Several replicas upgrade the same fresh database without racing DDL.
            def migrate(_):
                env = dict(os.environ, FDP_DATABASE_URL=urls[0], PYTHONPATH=str(ROOT/'services/control-plane'), PYTHONDONTWRITEBYTECODE='1')
                result = subprocess.run([sys.executable,'-B','-m','app.schema_migrations','upgrade'], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode:
                    raise RuntimeError('isolated migration subprocess failed')
            with ThreadPoolExecutor(max_workers=2) as workers:
                list(workers.map(migrate, range(2)))
            require_current(source)
            factory = sessionmaker(bind=source)
            private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            public = private.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo).decode()
            key = private.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            objects = {}
            class Body(io.BytesIO):
                def iter_chunks(self, chunk_size):
                    return iter(lambda:self.read(chunk_size), b'')
            class S3:
                def upload_file(self, path, bucket, object_key, ExtraArgs):
                    objects[(bucket, object_key)] = Path(path).read_bytes()
                def get_object(self, Bucket, Key):
                    return {'Body':Body(objects[(Bucket, Key)])}
            original = backups.client
            backups.client = lambda *a:S3()
            try:
                with factory() as db:
                    db.add(PlatformSetting(key=backups.SETTING, value={'bucket':'test-recovery', 'recovery_public_key':public}))
                    db.add(PlatformSetting(key='recovery.test.marker', value={'preserved':True}))
                    db.commit()
                    row = backups.queue(db, 'test:recovery'); ident = row.id; db.commit()
                settings = Settings(database_url=urls[0], orchestrator='fake', secrets_backend='memory')
                from app.secrets import MemorySecretStore
                store=MemorySecretStore()
                backups.run_one(factory, store, settings)
            finally:
                backups.client = original
            with factory() as db, tempfile.TemporaryDirectory(prefix='fdp-restore-test-') as temp:
                row = db.get(PlatformBackup, ident)
                assert row.status == 'succeeded', row.error
                root = Path(temp)
                archive = root/'archive.fdp'; archive.write_bytes(objects[(row.bucket,row.object_key)])
                with source.begin() as connection:
                    connection.execute(text('CREATE TABLE customer_business_data (value TEXT)'))
                try:
                    backups.dump_metadata(urls[0],root/'must-not-exist.dump')
                except ValueError:
                    assert not (root/'must-not-exist.dump').exists()
                else:
                    raise AssertionError('shared customer database was accepted as platform-only backup')
                finally:
                    with source.begin() as connection:
                        connection.execute(text('DROP TABLE customer_business_data'))
                envelope.decrypt(archive, root/'archive.tar', key, row.sha256)
                unpack(root/'archive.tar', root)
                restore(root, urls[1], store)
                require_current(target)
                restored = sessionmaker(bind=target)
                with restored() as db:
                    assert db.get(PlatformSetting,'recovery.test.marker').value == {'preserved':True}
                    assert backups.recovery_blocked(db)
                    assert not backups.incomplete_restore(db)
                    initial_epoch = db.get(PlatformSetting, backups.RECOVERY_EPOCH).value['id']
                # The same recovery gate is available after customer-native PITR.
                quarantine_native_restore(urls[1])
                with restored() as db:
                    assert backups.recovery_blocked(db)
                    assert db.get(PlatformSetting, backups.RECOVERY_EPOCH).value['id'] != initial_epoch
                # Reusing a populated target must refuse, preserving its existing state.
                try:
                    restore(root, urls[1], store)
                except ValueError as exc:
                    assert 'empty' in str(exc)
                else:
                    raise AssertionError('populated restore target was accepted')
            # Exercise the business-storage adapter separately from the metadata-only archive.
            from app import backup_adapters, storage_recovery
            from app.models import DataSource
            with source.begin() as connection:
                connection.execute(text('CREATE SCHEMA backup_business'))
                connection.execute(text('CREATE TABLE backup_business.orders (id INTEGER PRIMARY KEY, name TEXT)'))
                connection.execute(text("INSERT INTO backup_business.orders VALUES (1, 'native backup'), (2, 'verified restore')"))
            parsed=make_url(urls[0])
            with factory() as db, tempfile.TemporaryDirectory(prefix='fdp-native-storage-') as temp:
                item=DataSource(name='native-recovery-fixture',kind='postgres',config={
                    'host':parsed.host,'port':parsed.port or 5432,'database':parsed.database})
                db.add(item);db.flush()
                store.put('datasources/'+item.name+'/provisioner',{'username':parsed.username,
                    'password':parsed.password or 'disposable-trust-auth-fixture'})
                frozen={'id':item.id,'name':item.name,'kind':item.kind,'config':item.config,
                    'schemas':['backup_business'],'max_bytes':16*1024*1024}
                root=Path(temp);archive=root/'storage.tar'
                backup_adapters.bundle(db,frozen,store,root,archive)
                extracted=root/'verified';extracted.mkdir()
                manifest=storage_recovery.unpack(archive,extracted)
                assert manifest['method']=='postgresql-pg-dump'
                tool=backups.pg_tool('pg_restore',urls[1])
                result=subprocess.run([tool,'--single-transaction','--exit-on-error','--no-owner','--no-privileges',
                    '--dbname',make_url(urls[1]).database,str(extracted/'database.dump')],
                    env=backups.pg_environment(urls[1]),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
                if result.returncode:raise RuntimeError('isolated native integration restore failed')
            with target.connect() as connection:
                assert list(connection.execute(text('SELECT id,name FROM backup_business.orders ORDER BY id')))==[(1,'native backup'),(2,'verified restore')]
            # Managed Iceberg catalogs use the same JDBC tables as the layer's Trino.
            from app.iceberg_backup import JdbcCatalog
            from app.layer_postgres import SCHEMAS
            for engine in (source,target):
                with engine.begin() as connection:
                    for statement in SCHEMAS['iceberg'].split(';'):
                        if statement.strip():connection.execute(text(statement))
            catalog=JdbcCatalog(urls[0],'lake');recovered_catalog=JdbcCatalog(urls[1],'lake')
            try:
                tables=[{'namespace':['analytics'],'name':'orders',
                    'metadata_location':'s3://warehouse/analytics/orders/metadata/00001.metadata.json'}]
                catalog.register(tables,[['analytics'],['empty']])
                captured,namespaces=catalog.tables([],100)
                assert captured==tables and namespaces==[['analytics'],['empty']]
                recovered_catalog.check(captured)
                recovered_catalog.register(captured,namespaces)
                recovered_catalog.register(captured,namespaces)  # idempotent
                assert recovered_catalog.tables([],100)==(captured,namespaces)
                try:
                    recovered_catalog.register([{**tables[0],'metadata_location':'s3://warehouse/conflicting.metadata.json'}],namespaces)
                except ValueError:pass
                else:raise AssertionError('conflicting JDBC catalog pointer was overwritten')
                assert recovered_catalog.tables([],100)==(captured,namespaces)
            finally:
                catalog.close();recovered_catalog.close()
            assert store.read('platform/runtime')
            print('PASS: serialized real PostgreSQL migrations, pg_dump, encrypted S3 adapter read-back, authenticated decryption, pg_restore, preserved metadata, vault-only runtime keys, native storage schema backup/restore, managed Iceberg JDBC catalog capture/recovery and recovery quarantine; populated restore refused')
        finally:
            for engine in engines:
                engine.dispose()
            for name in databases:
                admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__ == '__main__':
    main()
