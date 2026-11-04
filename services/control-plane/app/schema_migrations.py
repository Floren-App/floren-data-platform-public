"""Explicit, serialized metadata schema upgrades. Web processes only check the revision."""
from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import sqlalchemy as sa
from alembic import command, context
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic import op

ROOT = Path(__file__).with_name('alembic')
LOCAL_LOCK = threading.RLock()


def config(connection=None, adopt_legacy=False):
    cfg = Config()
    cfg.set_main_option('script_location', str(ROOT))
    cfg.attributes.update(connection=connection, adopt_legacy=adopt_legacy)
    return cfg


def head():
    return ScriptDirectory.from_config(config()).get_current_head()


def current(connection):
    return MigrationContext.configure(connection).get_current_revision()


def require_current(engine):
    with engine.connect() as connection:
        if current(connection) != head():
            raise RuntimeError('Metadata schema upgrade required: run python -m app.schema_migrations upgrade before starting FDP')


def upgrade(engine, adopt_legacy=False):
    with LOCAL_LOCK, engine.begin() as connection:
        if connection.dialect.name == 'postgresql':
            connection.execute(sa.text("SET LOCAL lock_timeout = '60s'"))
            connection.execute(sa.text('SELECT pg_advisory_xact_lock(73410892)'))
        tables = set(sa.inspect(connection).get_table_names()) - {'alembic_version'}
        if current(connection) is None and tables and not adopt_legacy:
            raise RuntimeError('Unversioned metadata requires a backup and explicit --adopt-legacy before first upgrade')
        command.upgrade(config(connection, adopt_legacy), 'head')
        from sqlalchemy.orm import sessionmaker
        from .migrations import upgrade as upgrade_data, upgrade_git_credentials
        factory = sessionmaker(bind=connection)
        upgrade_data(factory)
        upgrade_git_credentials(factory)
        from .role_migration import upgrade_role_model
        upgrade_role_model(factory)


def baseline_table(name, *elements, **kwargs):
    """One-time legacy adoption using the frozen baseline, never live ORM definitions."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(name):
        return op.create_table(name, *elements, **kwargs)
    if not context.config.attributes.get('adopt_legacy'):
        raise RuntimeError('Existing unversioned table requires explicit legacy adoption')
    existing = {c['name']:c for c in inspector.get_columns(name)}
    if name == 'compute_pools' and 'max_concurrent_runs' in existing and 'max_concurrent' not in existing:
        op.alter_column(name, 'max_concurrent_runs', new_column_name='max_concurrent')
        existing['max_concurrent'] = existing.pop('max_concurrent_runs')
    defaults = json.loads((ROOT/'legacy_defaults.json').read_text()).get(name, {})
    for element in elements:
        if not isinstance(element, sa.Column) or element.name in existing:
            continue
        default = defaults.get(element.name)
        column = sa.Column(element.name, element.type, nullable=True)
        op.add_column(name, column)
        if element.name in defaults:
            table = sa.table(name, sa.column(element.name, element.type))
            bind.execute(table.update().values({element.name:default}))
        elif not element.nullable:
            # The transaction rolls back on PostgreSQL. Do not invent privileged or identity values.
            count = bind.scalar(sa.select(sa.func.count()).select_from(sa.table(name)))
            if count:
                raise RuntimeError('Legacy metadata requires an explicit backfill for '+name+'.'+element.name)
        if not element.nullable and bind.dialect.name == 'postgresql':
            op.alter_column(name, element.name, nullable=False)


def baseline_index(name, table_name, columns, **kwargs):
    if name not in {i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table_name)}:
        op.create_index(name, table_name, columns, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['upgrade','check'])
    parser.add_argument('--adopt-legacy', action='store_true')
    args = parser.parse_args()
    from .db import engine
    try:
        if args.operation == 'upgrade':
            upgrade(engine, args.adopt_legacy)
        require_current(engine)
        print('Metadata schema revision:', head())
    except Exception as exc:
        # Database exceptions can contain connection credentials and SQL payloads.
        parser.exit(1, 'Metadata schema operation failed ('+type(exc).__name__+'). Consult the upgrade runbook.\n')


if __name__ == '__main__':
    main()
