"""Offline SQLite -> PostgreSQL metadata copy for the SAME Airflow schema version.

Run inside the matching Airflow runtime after stopping the old deployment and
running `airflow db migrate` against a new private PostgreSQL database. Target URL
comes from AIRFLOW__DATABASE__SQL_ALCHEMY_CONN, never a command-line argument.
Preserve the old Fernet key separately. Keep the SQLite backup for rollback.
"""
import argparse
import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import MetaData, create_engine, inspect, select, text


def canonical(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec='microseconds')
    if isinstance(value, (bytes, memoryview)):
        return {'bytes': base64.b64encode(bytes(value)).decode()}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k:canonical(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [canonical(v) for v in value]
    return value


def digest(rows):
    values=sorted(json.dumps(canonical(dict(row)),sort_keys=True,separators=(',',':')) for row in rows)
    return hashlib.sha256('\n'.join(values).encode()).hexdigest()


def migrate(source, target):
    """Atomic target replacement with preflight, row-level verification and sequence repair."""
    if source.dialect.name!='sqlite' or target.dialect.name!='postgresql':
        raise ValueError('requires a read-only SQLite source and PostgreSQL target')
    sm,tm=MetaData(),MetaData()
    sm.reflect(bind=source,only=lambda name,meta:not name.startswith('sqlite_'));tm.reflect(bind=target)
    if set(sm.tables)!=set(tm.tables):
        raise ValueError('source and target table sets differ; use the same Airflow runtime')
    with source.connect() as src, target.begin() as dst:
        dst.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        if src.execute(text('SELECT version_num FROM alembic_version')).fetchall()!=dst.execute(text('SELECT version_num FROM alembic_version')).fetchall():
            raise ValueError('Airflow migration versions differ')
        # A newly initialized database has only migration/default-pool/log-template seeds.
        seeds={'alembic_version','slot_pool','log_template'}
        for name,table in tm.tables.items():
            if name not in seeds and dst.execute(select(table).limit(1)).first() is not None:
                raise ValueError('target is not empty: '+name)
            if set(sm.tables[name].columns.keys())!=set(table.columns.keys()):
                raise ValueError('column sets differ: '+name)
        quote=target.dialect.identifier_preparer.quote
        dst.execute(text('TRUNCATE TABLE '+', '.join(quote(t.name) for t in tm.sorted_tables)+' CASCADE'))
        counts={}
        for table in tm.sorted_tables:
            rows=[dict(row) for row in src.execute(select(sm.tables[table.name])).mappings()]
            for start in range(0,len(rows),500):
                dst.execute(table.insert(),rows[start:start+500])
            actual=[dict(row) for row in dst.execute(select(table)).mappings()]
            if len(actual)!=len(rows) or digest(rows)!=digest(actual):
                raise ValueError('copied data differs: '+table.name)
            counts[table.name]=len(rows)
            for col in table.columns:
                sequence=dst.execute(text('SELECT pg_get_serial_sequence(:table,:column)'),
                                     {'table':table.name,'column':col.name}).scalar()
                if sequence:
                    maximum=max((row[col.name] for row in rows if row[col.name] is not None),default=None)
                    dst.execute(text('SELECT setval(CAST(:seq AS regclass),:value,:called)'),
                                {'seq':sequence,'value':maximum or 1,'called':maximum is not None})
        return counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite',required=True)
    parser.add_argument('--writers-stopped',action='store_true',required=True)
    args=parser.parse_args()
    path=Path(args.sqlite).resolve(strict=True)
    source=create_engine('sqlite:///'+path.as_uri()+'?mode=ro&uri=true')
    target=create_engine(os.environ['AIRFLOW__DATABASE__SQL_ALCHEMY_CONN'])
    try:
        counts=migrate(source,target)
        print(json.dumps({'tables_verified':len(counts),'rows_copied':sum(counts.values()),
                          'dag_runs':counts.get('dag_run',0),'task_instances':counts.get('task_instance',0),
                          'all_content_hashes_match':True}))
    finally:
        source.dispose();target.dispose()

if __name__=='__main__':main()
