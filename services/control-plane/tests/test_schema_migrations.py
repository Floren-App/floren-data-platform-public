import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app import models
from app.db import Base
from app.schema_migrations import upgrade, require_current, head


def test_fresh_schema_matches_models_and_upgrade_is_idempotent():
    engine = create_engine('sqlite://')
    upgrade(engine); require_current(engine); upgrade(engine)
    with engine.connect() as connection:
        diffs = compare_metadata(MigrationContext.configure(connection, opts={
            'include_object':lambda obj,name,kind,*args: name not in ('fdp_migrations','alembic_version')}), Base.metadata)
        assert diffs == []


def test_upgrade_expires_unbound_kafka_sessions_and_registers_imported_packages():
    from alembic import command
    from app.schema_migrations import config
    engine=create_engine('sqlite://')
    with engine.begin() as connection:
        command.upgrade(config(connection),'0003_named_data_snapshot')
        connection.execute(text("INSERT INTO kafka_sessions "
            "(id,principal_id,username,layer_id,topic_id,mode,consumer_group,execution_context,expires_at) "
            "VALUES ('old-session','person','ana','dev','topic','consume','group','',9999999999)"))
        connection.execute(models.Package.__table__.insert().values(id='imported',kind='python',key='example',
            name='example',origin='pypi',resource_id=None,created_by='operator'))
    upgrade(engine)
    with engine.connect() as connection:
        row=connection.execute(text('SELECT active_group_id,expires_at FROM kafka_sessions')).one()
        assert row==('',0)
        target=connection.scalar(text("SELECT resource_id FROM packages WHERE id='imported'"))
        assert target
        assert connection.scalar(text('SELECT parent_id FROM rbac_resources WHERE id=:target'),{'target':target})=='packages'
        assert not connection.scalar(text('SELECT count(*) FROM role_assignments WHERE resource_id=:target'),{'target':target})


def test_unversioned_install_requires_explicit_adoption_and_preserves_records():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(models.PlatformSetting.__table__.insert().values(key='customer.setting', value={'kept':True}))
    with pytest.raises(RuntimeError, match='adopt-legacy'):
        upgrade(engine)
    upgrade(engine, adopt_legacy=True)
    require_current(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM platform_settings WHERE key='customer.setting'")).scalar()


def test_pending_or_future_schema_refuses_web_startup():
    engine = create_engine('sqlite://')
    with pytest.raises(RuntimeError, match='upgrade required'):
        require_current(engine)
    upgrade(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='future_revision'"))
    with pytest.raises(RuntimeError, match='upgrade required'):
        require_current(engine)
    with pytest.raises(Exception):
        upgrade(engine)


def test_upgrade_from_061_baseline_adds_recovery_inventory():
    from alembic import command
    from app.schema_migrations import config
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        command.upgrade(config(connection), '0001_061_baseline')
        connection.execute(models.PlatformSetting.__table__.insert().values(key='before.upgrade', value={'kept':True}))
    assert 'platform_backups' not in inspect(engine).get_table_names()
    upgrade(engine)
    assert 'platform_backups' in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM platform_settings WHERE key='before.upgrade'")).scalar()
