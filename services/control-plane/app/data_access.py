"""Native data authorization and leased PostgreSQL connections.

Other engines are explicitly unavailable for governed credential issuance until a native
adapter exists. No shared credential fallback can bypass a schema role.
"""
from __future__ import annotations
import hashlib
import secrets
from contextlib import contextmanager
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select
from .models import CredentialLease, DataSource, Resource, Run, utcnow
from .config import get_settings
from . import rbac


def role_names(source_id, schema):
    key = hashlib.sha256(f"{source_id}:{schema}".encode()).hexdigest()[:16]
    return {"Reader": f"dc_{key}_ro", "Contributor": f"dc_{key}_rw", "Admin": f"dc_{key}_ddl",
            "Owner": f"dc_{key}_ddl"}


@contextmanager
def manager(source, store):
    import psycopg
    cred = store.read(f"datasources/{source.name}/provisioner")
    if not cred.get("username") or not cred.get("password"):
        raise HTTPException(409, "configure a separate PostgreSQL provisioning credential first")
    cfg = source.config
    conn = psycopg.connect(host=cfg.get("host"), port=cfg.get("port", 5432),
                          dbname=cfg.get("database"), user=cred["username"], password=cred["password"],
                          connect_timeout=10, **({"sslmode": cfg["sslmode"]} if cfg.get("sslmode") else {}))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def provision_schema(source, resource, store):
    from .config import get_settings
    if get_settings().query_executor == "trino" and source.kind in ("iceberg", "trino", "mysql", "mssql", "snowflake"):
        from .sql_access import catalog_name
        resource.config = {**resource.config, "state": "ready", "executor": "trino", "catalog": catalog_name(source)}
        return
    if source.kind == "s3":
        from .storage_access import prefix_path
        prefix = prefix_path(source.config.get("prefix", ""), resource.config.get("prefix", resource.name))
        resource.config = {**resource.config, "state": "ready", "storage_prefix": prefix}
        return
    if source.kind != "postgres":
        raise HTTPException(409, f"native schema provisioning is not yet implemented for {source.kind}")
    from psycopg import sql
    I = sql.Identifier
    names = role_names(source.id, resource.name)
    with manager(source, store) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s", (resource.name,))
            existing_owner = cur.fetchone()
            if not existing_owner:
                raise HTTPException(422, "schema does not exist; create it with the database administrator first")
            if existing_owner[0].startswith("dc_") and existing_owner[0].endswith("_ddl") and existing_owner[0] != names["Admin"]:
                raise HTTPException(409, "this schema is already governed through another source; use one schema authority")
            # Do not elevate an execution into a server/database-wide admin.
            for name in set(names.values()):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,))
                if not cur.fetchone():
                    cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION").format(I(name)))
            owner, reader, writer = names["Admin"], names["Reader"], names["Contributor"]
            cur.execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(I(resource.name), I(owner)))
            cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(I(resource.name)))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(I(resource.name), I(reader), I(writer)))
            # Existing objects need common ownership so native DDL works consistently.
            cur.execute("SELECT c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','S','f') ORDER BY (c.relkind='S')", (resource.name,))
            for name, kind in cur.fetchall():
                typ = {"S": "SEQUENCE", "v": "VIEW", "m": "MATERIALIZED VIEW", "f": "FOREIGN TABLE"}.get(kind, "TABLE")
                cur.execute(sql.SQL("ALTER " + typ + " {}.{} OWNER TO {}").format(I(resource.name), I(name), I(owner)))
            cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(I(resource.name)))
            cur.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC").format(I(resource.name)))
            cur.execute(sql.SQL("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA {} FROM PUBLIC").format(I(resource.name)))
            cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}, {}").format(I(resource.name), I(reader), I(writer)))
            cur.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}, {}").format(I(resource.name), I(reader), I(writer)))
            cur.execute(sql.SQL("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA {} FROM {}, {}").format(I(resource.name), I(reader), I(writer)))
            cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(I(resource.name), I(reader)))
            cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(I(resource.name), I(writer)))
            cur.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(I(resource.name), I(writer)))
            for target, objects, privileges in ((reader, "TABLES", "SELECT"),
                    (writer, "TABLES", "SELECT, INSERT, UPDATE, DELETE"), (writer, "SEQUENCES", "USAGE, SELECT")):
                cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT " + privileges +
                                    " ON " + objects + " TO {}").format(I(owner), I(resource.name), I(target)))
            cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC").format(I(owner)))
            if get_settings().query_executor == "trino":
                # Engine-only account. It inherits object ownership for DDL without
                # server/database-wide role administration or credentials in executions.
                engine = store.read(f"datasources/{source.name}")
                username, password = engine.get("username"), engine.get("password")
                if not username or not password:
                    raise HTTPException(409, "configure the engine-only PostgreSQL connection credential first")
                cur.execute("SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=%s", (username,))
                attributes = cur.fetchone()
                if attributes and (not attributes[0] or any(attributes[1:])):
                    raise HTTPException(409, "Trino account must be a login without superuser, CREATEDB, or CREATEROLE")
                if not attributes:
                    cur.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}")
                                .format(I(username), sql.Literal(password)))
                cur.execute(sql.SQL("GRANT {} TO {}").format(I(owner), I(username)))
                cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC").format(I(username)))
    resource.config = {**resource.config, "state": "ready", "native_roles": names,
                       "native_admin_grants": True,
                       "future_object_owner": owner}
    if get_settings().query_executor == "trino":
        resource.config = {**resource.config, "executor": "trino"}


def snapshot(db, user):
    return {ds.id: rbac.data_scope(db, user, ds) for ds in db.scalars(select(DataSource))
            if rbac.data_scope(db, user, ds)}


def scope_for_execution(db, execution, source):
    if execution.run_id:
        run = db.get(Run, execution.run_id)
        if not run or run.status not in ("pending", "running") or run.owner != execution.username:
            raise HTTPException(401, "run is no longer active")
        return (run.access_snapshot or {}).get(source.id, {})
    from .models import Workload
    if execution.workload_id:
        pub = db.get(Workload, execution.workload_id)
        if not pub or pub.status == "stopped" or (pub.execution_mode == "caller" and pub.kind == "app"):
            return {}
    from .auth import User
    from .environments import execution_user
    return rbac.data_scope(db, rbac.resolve_user(db, execution_user(db, execution)), source)


def credentials(db, source, scope, store, principal, run_id=None):
    if not scope:
        raise HTTPException(404, "data source not available")
    from psycopg import sql
    parent = rbac.source_resource(db, source)
    schemas = {r.name: r for r in db.scalars(select(Resource).where(Resource.parent_id == parent.id, Resource.kind == "schema"))}
    if any(name not in schemas or schemas[name].config.get("state") != "ready" for name in scope):
        raise HTTPException(409, "native schema permissions must be provisioned before connecting")
    if source.kind == "s3":
        from .storage_types import lake_buckets
        if get_settings().query_executor == "trino" and source.config.get("bucket") in lake_buckets(db):
            raise HTTPException(409, "Iceberg buckets are accessible only through governed SQL")
        from .storage_access import credentials as storage_credentials
        return storage_credentials(source, schemas, scope, store, db=db)
    if source.kind != "postgres":
        raise HTTPException(409, f"governed credentials for {source.kind} require a native adapter; shared credentials are disabled")
    from .data_policies import schema_has_policies
    if schema_has_policies(db, [schemas[name].id for name in scope]):
        # A native login would read past column masks and row filters, which only the SQL engine enforces.
        raise HTTPException(409, "schemas with column masks or row filters are reachable only through governed SQL")
    ddl_schemas =[schema for schema, role in scope.items() if role in ("Admin", "Owner")]
    if len(ddl_schemas) > 1:
        raise HTTPException(409, "select one schema when connecting with DDL authority")
    username, password = "dcl_" + secrets.token_hex(12), secrets.token_urlsafe(32)
    expiry = utcnow() + timedelta(seconds=get_settings().data_credential_ttl_seconds)
    with manager(source, store) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {} VALID UNTIL {}")
                        .format(sql.Identifier(username), sql.Literal(password), sql.Literal(expiry.isoformat())))
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(source.config["database"]), sql.Identifier(username)))
            for schema, role in scope.items():
                native = role_names(source.id, schema)[role]
                cur.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(native), sql.Identifier(username)))
            if ddl_schemas:
                # Login retains its other authorized schema memberships. Give new objects
                # default grants, then reassign their ownership on lease cleanup.
                schema = ddl_schemas[0]
                names = role_names(source.id, schema)
                for target, objects, privileges in ((names["Reader"], "TABLES", "SELECT"),
                        (names["Contributor"], "TABLES", "SELECT, INSERT, UPDATE, DELETE"),
                        (names["Contributor"], "SEQUENCES", "USAGE, SELECT")):
                    cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT " + privileges +
                                        " ON " + objects + " TO {}").format(
                                            sql.Identifier(username), sql.Identifier(schema), sql.Identifier(target)))
                cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC").format(sql.Identifier(username)))
            cur.execute(sql.SQL("ALTER ROLE {} SET search_path TO {}").format(
                sql.Identifier(username), sql.SQL(", ").join([sql.Identifier(name) for name in scope] + [sql.Identifier("pg_catalog")])))
            cur.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
                        "AND NOT (n.nspname = ANY(%s)) AND c.relkind IN ('r','p','v','m','f') "
                        "AND has_schema_privilege(%s, n.oid, 'USAGE') "
                        "AND has_table_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') LIMIT 1",
                        (list(scope), username, username))
            escaped = bool(cur.fetchone())
            cur.execute("SELECT 1 FROM pg_namespace n WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
                        "AND NOT (n.nspname = ANY(%s)) AND has_schema_privilege(%s, n.oid, 'CREATE') LIMIT 1",
                        (list(scope), username))
            if escaped or cur.fetchone():
                raise HTTPException(409, "native PUBLIC grants expose data outside the assigned schemas; reconcile database permissions first")
            for schema, level in scope.items():
                if level in ("Admin", "Owner"):
                    continue
                cur.execute("SELECT has_schema_privilege(%s, %s, 'CREATE')", (username, schema))
                unsafe = cur.fetchone()[0]
                unexpected = "INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER" if level == "Reader" else "TRUNCATE,REFERENCES,TRIGGER"
                cur.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') "
                            "AND (has_table_privilege(%s,c.oid,%s) OR pg_has_role(%s,c.relowner,'USAGE')) LIMIT 1",
                            (schema, username, unexpected, username))
                unsafe = unsafe or bool(cur.fetchone())
                cur.execute("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname=%s AND has_function_privilege(%s,p.oid,'EXECUTE') LIMIT 1", (schema, username))
                unsafe = unsafe or bool(cur.fetchone())
                if unsafe:
                    raise HTTPException(409, "native privilege drift exceeds the assigned role; reprovision schema permissions before connecting")
    db.add(CredentialLease(source_id=source.id, username=username, principal=principal,
                           run_id=run_id or None, expires_at=expiry,
                           object_owner_role=role_names(source.id, ddl_schemas[0])["Admin"] if ddl_schemas else None))
    db.commit()
    return {"username": username, "password": password, "lease_seconds": str(get_settings().data_credential_ttl_seconds),
            "expires_at": expiry.isoformat()}, {"schemas": scope,
            "native_roles": {schema: role_names(source.id, schema)[role] for schema, role in scope.items()}}


def cleanup_leases(db, store):
    from psycopg import sql
    now = utcnow()
    for lease in db.scalars(select(CredentialLease).where(CredentialLease.revoked.is_(False))):
        run = db.get(Run, lease.run_id) if lease.run_id else None
        expired = lease.expires_at.replace(tzinfo=now.tzinfo) <= now
        finished = lease.run_id and (not run or run.status not in ("pending", "running"))
        if run and run.status == "running" and expired:
            # Retain precisely this job's authority until it finishes; independent interactive
            # identities expire normally. Refresh the DB login validity, not the role scope.
            with manager(db.get(DataSource, lease.source_id), store) as conn:
                with conn.cursor() as cur:
                    lease.expires_at = now + timedelta(seconds=get_settings().data_credential_ttl_seconds)
                    cur.execute(sql.SQL("ALTER ROLE {} VALID UNTIL {}").format(
                        sql.Identifier(lease.username), sql.Literal(lease.expires_at.isoformat())))
            db.commit()
            continue
        if not expired and not finished:
            continue
        ds = db.get(DataSource, lease.source_id)
        try:
            with manager(ds, store) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s AND pid<>pg_backend_pid()", (lease.username,))
                    if lease.object_owner_role:
                        # A native Admin can RESET ROLE and create an object as its login.
                        # Preserve those objects when retiring the ephemeral credential.
                        cur.execute(sql.SQL("REASSIGN OWNED BY {} TO {}").format(
                            sql.Identifier(lease.username), sql.Identifier(lease.object_owner_role)))
                    cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(lease.username)))
                    cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(lease.username)))
            lease.revoked = True
            db.commit()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("credential cleanup failed for lease %s", lease.id)
