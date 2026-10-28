"""Opt-in real PostgreSQL 16+ isolation check (temporary databases/roles only).

FDP_TEST_AIRFLOW_ADMIN_URL must point to an administrator on a test PostgreSQL
server. All created names are random and removed even when assertions fail.
"""
import os
import secrets

import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.skipif(not os.environ.get('FDP_TEST_AIRFLOW_ADMIN_URL'),
                                reason='requires a PostgreSQL integration server')


def test_non_superuser_provisioning_is_idempotent_and_tenants_cannot_cross_databases(monkeypatch):
    import psycopg
    from psycopg import sql
    from app.airflow_db import ensure_database
    from app.config import get_settings
    from app.secrets import MemorySecretStore
    root = make_url(os.environ['FDP_TEST_AIRFLOW_ADMIN_URL']).set(drivername='postgresql')
    suffix = secrets.token_hex(5)
    provisioner = 'fdp_probe_' + suffix
    password = secrets.token_urlsafe(24)
    ids = ['probe_' + suffix + '_a', 'probe_' + suffix + '_b']
    names = ['airflow_' + i for i in ids]
    store = MemorySecretStore()
    with psycopg.connect(root.render_as_string(hide_password=False), autocommit=True) as admin:
        try:
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT CREATEDB CREATEROLE PASSWORD {}').format(
                sql.Identifier(provisioner), sql.Literal(password)))
            monkeypatch.setattr(get_settings(), 'airflow_metadata_url',
                root.set(username=provisioner,password=password).render_as_string(hide_password=False))
            urls = [make_url(ensure_database(store,p)).set(drivername='postgresql') for p in ids]
            assert make_url(ensure_database(store,ids[0])).database == names[0]
            for index, url in enumerate(urls):
                with psycopg.connect(url.render_as_string(hide_password=False)) as tenant:
                    tenant.execute('CREATE TABLE isolation_probe (id int)')
                    tenant.execute('INSERT INTO isolation_probe VALUES (1)')
                with pytest.raises(psycopg.OperationalError, match='permission denied for database'):
                    psycopg.connect(url.set(database=names[1-index]).render_as_string(hide_password=False))
                # A tenant cannot administer roles or create more databases.
                flags = admin.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls '
                                      'FROM pg_roles WHERE rolname=%s',(names[index],)).fetchone()
                assert flags == (False,False,False,False,False)
                public_connect = admin.execute("SELECT has_database_privilege(%s,%s,'CONNECT')",
                                               (names[index],names[1-index])).fetchone()[0]
                assert public_connect is False
        finally:
            for name in names:
                admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
                admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(name)))
            admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(provisioner)))


def test_offline_copy_preserves_typed_rows_sequences_and_rejects_a_nonempty_target(tmp_path):
    import importlib.util
    from pathlib import Path
    from datetime import datetime, timezone
    import psycopg
    from psycopg import sql
    from sqlalchemy import MetaData, Table, Column, Integer, String, Boolean, DateTime, JSON, LargeBinary, ForeignKey, create_engine, select
    path=Path(__file__).resolve().parents[3]/'deploy/airflow/migrate_metadata.py'
    spec=importlib.util.spec_from_file_location('metadata_copy',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    root=make_url(os.environ['FDP_TEST_AIRFLOW_ADMIN_URL']).set(drivername='postgresql')
    name='airflow_copy_probe_'+secrets.token_hex(5)
    source=create_engine('sqlite:///'+str(tmp_path/'metadata.db'))
    target=None
    with psycopg.connect(root.render_as_string(hide_password=False),autocommit=True) as admin:
        try:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            target=create_engine(root.set(drivername='postgresql+psycopg',database=name))
            m=MetaData()
            version=Table('alembic_version',m,Column('version_num',String(32),primary_key=True))
            parent=Table('parent',m,Column('id',Integer,primary_key=True),Column('enabled',Boolean),
                Column('created_at',DateTime(timezone=True)),Column('payload',JSON),Column('blob',LargeBinary),sqlite_autoincrement=True)
            child=Table('child',m,Column('id',Integer,primary_key=True),Column('parent_id',ForeignKey('parent.id')))
            m.create_all(source);m.create_all(target)
            with source.begin() as c:
                c.execute(version.insert(),{'version_num':'same-version'})
                c.execute(parent.insert(),{'id':7,'enabled':True,'created_at':datetime(2026,1,1,tzinfo=timezone.utc),
                    'payload':{'records':[{'id':1}]},'blob':b'\x00\x01'})
                c.execute(child.insert(),{'id':3,'parent_id':7})
            with target.begin() as c:c.execute(version.insert(),{'version_num':'same-version'})
            assert module.migrate(source,target)=={'alembic_version':1,'parent':1,'child':1}
            with target.begin() as c:
                assert c.execute(parent.insert().values(enabled=False).returning(parent.c.id)).scalar_one()==8
            with pytest.raises(ValueError,match='target is not empty'):
                module.migrate(source,target)
            with target.connect() as c:assert len(c.execute(select(parent)).fetchall())==2
        finally:
            source.dispose()
            if target is not None:target.dispose()
            admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
