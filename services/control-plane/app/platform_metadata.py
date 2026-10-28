"""Protected, live SQL inventory of the control plane's own database."""
import re

from fastapi import HTTPException
from sqlalchemy import select

from .config import get_settings
from .db import Base
from .models import DataSource, Resource
from .ownership import GLOBAL_ADMIN

SOURCE_ID = "000000000002"
SCHEMA_ID = "000000000003"
SOURCE_NAME = "fdp-metadata"


def is_resource(resource):
    return bool(resource and resource.object_id == SOURCE_ID and resource.kind in {"source", "schema"})


def authorized(user):
    return bool(user.is_admin and not user.execution_context and user.active_group_id == GLOBAL_ADMIN)


def require(user):
    if not authorized(user):
        raise HTTPException(403, "Select Global Admin to query or manage platform metadata. Execution and service identities cannot access it.")


def table_names():
    return sorted(set(Base.metadata.tables) | {"fdp_migrations"})


def setup(db):
    """Idempotent deployment setup, using the configured metadata database itself."""
    settings = get_settings()
    source = db.get(DataSource, SOURCE_ID)
    if not settings.metadata_sql_enabled:
        return
    if db.bind.dialect.name != "postgresql":
        raise RuntimeError("The metadata SQL catalog requires a PostgreSQL FDP_DATABASE_URL")
    for name in (settings.metadata_catalog, settings.metadata_schema, settings.metadata_reader_user):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) or name in {"system", "information_schema", "pg_catalog"}:
            raise RuntimeError("Metadata SQL names must be non-system lowercase SQL identifiers")
    # The application currently stores its tables in public. Reject a misleading
    # deployment rather than silently exposing an unrelated schema.
    if settings.metadata_schema != "public":
        raise RuntimeError("The operational metadata schema is public; configure the database and catalog, not a different schema")
    if settings.metadata_bootstrap_reader:
        if not settings.metadata_reader_password or not settings.metadata_reader_user.startswith("fdp_metadata_"):
            raise RuntimeError("Configure a dedicated fdp_metadata_* reader and its password")
        from psycopg import sql
        # A separate connection keeps role DDL and credentials out of SQLAlchemy SQL
        # logging. Identifiers and password are quoted by the driver, never interpolated.
        with db.bind.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            with connection.connection.driver_connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (settings.metadata_reader_user,))
                role = sql.Identifier(settings.metadata_reader_user)
                if cursor.fetchone() is None:
                    cursor.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
                cursor.execute(sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION PASSWORD {}").format(
                    role, sql.Literal(settings.metadata_reader_password)))
                cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db.bind.url.database), role))
                cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
                cursor.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role))
                cursor.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
    from . import rbac
    url = db.bind.url
    if not source:
        if db.scalar(select(DataSource.id).where(DataSource.name == SOURCE_NAME)):
            raise RuntimeError("fdp-metadata is reserved for the platform metadata catalog")
        source = DataSource(id=SOURCE_ID, name=SOURCE_NAME, kind="postgres")
        db.add(source)
    source.description = "Floren Data Platform platform metadata. Global Admin only; SQL changes use validated management operations."
    source.config = {"system_metadata": True, "catalog": settings.metadata_catalog, "host": url.host,
                     "port": url.port or 5432, "database": url.database}
    source.egress = []
    db.flush()
    parent = rbac.source_resource(db, source)
    parent.config = {"system_metadata": True, "source_kind": "postgres", "owner_group_id": GLOBAL_ADMIN}
    schema = db.get(Resource, SCHEMA_ID)
    if not schema:
        schema = Resource(id=SCHEMA_ID, kind="schema", parent_id=parent.id, object_id=source.id)
        db.add(schema)
    schema.name = settings.metadata_schema
    schema.config = {"system_metadata": True, "source_kind": "postgres", "executor": "trino",
                     "catalog": settings.metadata_catalog, "state": "ready", "owner_group_id": GLOBAL_ADMIN}
    db.flush()
    # Our own schema is already known. This never crawls external storage.
    from .catalog import upsert
    for name in table_names():
        table = Base.metadata.tables.get(name)
        columns = [{"name": c.name, "data_type": str(c.type), "nullable": c.nullable}
                   for c in table.columns] if table is not None else []
        upsert(db, schema, key="object:" + name, kind="table", name=name,
               path=SOURCE_NAME + " / " + schema.name + " / " + name,
               attributes={"system_metadata": True, "columns": columns})
