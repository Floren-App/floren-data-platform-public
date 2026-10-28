"""Metadata databases and keys for Airflow deployments.

Each shared-server deployment gets its own database and login on the server admins configure
(FDP_AIRFLOW_METADATA_URL, a PostgreSQL URL of a role that may create databases and roles).
The password, Airflow's Fernet key and webserver secret live in OpenBao at airflow/<workload>.
Databases are kept on undeploy (run history); drop them deliberately when no longer needed.
"""

from __future__ import annotations

import secrets as pysecrets
from typing import Dict
from urllib.parse import quote
import re
import hashlib

from sqlalchemy.engine import make_url

from .config import get_settings
from .secrets import SecretStore


class AirflowDatabaseError(Exception):
    pass


def keys_path(pub_id: str) -> str:
    return f"airflow/{pub_id}"


def deployment_keys(store: SecretStore, pub_id: str) -> Dict[str, str]:
    """Stable per deployment: every component must share the same Fernet key and secret.

    Keys added later (the broker password) are filled in once; existing ones never change."""
    from cryptography.fernet import Fernet

    keys = dict(store.read(keys_path(pub_id)) or {})
    wanted = {"password": lambda: pysecrets.token_urlsafe(24), "fernet_key": lambda: Fernet.generate_key().decode(),
              "secret_key": lambda: pysecrets.token_urlsafe(32), "broker_password": lambda: pysecrets.token_urlsafe(24)}
    missing = {name: make() for name, make in wanted.items() if not keys.get(name)}
    if missing:
        keys.update(missing)
        store.put(keys_path(pub_id), keys)
    return keys


def layer_database_url(pub_id: str, keys: Dict[str, str]) -> str:
    """The deployment's own metadata database inside its managed layer (see executions.airflow_metadata_spec)."""
    return f"postgresql+psycopg2://airflow:{quote(keys['password'], safe='')}@pub-{pub_id}-metadata:5432/airflow"


def shared_database_url(pub_id: str, keys: Dict[str, str], driver="postgresql+psycopg2"):
    """Only this deployment's login; preserve external TLS/connection options."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", pub_id):
        raise AirflowDatabaseError("invalid Airflow deployment identifier")
    name = "airflow_" + pub_id
    return make_url(get_settings().airflow_metadata_url).set(drivername=driver, username=name,
        password=keys["password"], database=name)


def ensure_database(store: SecretStore, pub_id: str) -> str:
    """Provision a private database/login on the shared server, never platform credentials."""
    admin_url = get_settings().airflow_metadata_url
    if not admin_url:
        raise AirflowDatabaseError("shared Airflow requires FDP_AIRFLOW_METADATA_URL or split connection fields")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", pub_id):
        raise AirflowDatabaseError("invalid Airflow deployment identifier")
    keys = deployment_keys(store, pub_id)
    name = "airflow_" + pub_id
    import psycopg
    from psycopg import sql

    # CREATE DATABASE cannot run inside a transaction. Serialize its check/create across replicas.
    lock = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], 'big', signed=True)
    try:
        url = make_url(admin_url).set(drivername='postgresql').render_as_string(hide_password=False)
        with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
            conn.execute("SET statement_timeout = '30s'")
            conn.execute("SELECT pg_advisory_lock(%s)", (lock,))
            role = conn.execute("SELECT rolsuper, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
            if role and any(role):
                raise AirflowDatabaseError("Airflow login has unexpected administrative privileges")
            stmt = ("ALTER ROLE {} LOGIN NOINHERIT NOCREATEDB NOCREATEROLE "
                    "CONNECTION LIMIT 32 PASSWORD {}") if role else (
                    "CREATE ROLE {} LOGIN NOINHERIT NOCREATEDB NOCREATEROLE "
                    "CONNECTION LIMIT 32 PASSWORD {}")
            conn.execute(sql.SQL(stmt).format(sql.Identifier(name), sql.Literal(keys["password"])))
            # PostgreSQL 16 gives the creator ADMIN but not necessarily SET membership.
            # Provisioning the database with this owner requires SET ROLE permission.
            grant = "GRANT {} TO CURRENT_USER" + (" WITH SET TRUE" if conn.info.server_version >= 160000 else "")
            conn.execute(sql.SQL(grant).format(sql.Identifier(name)))
            owner = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (name,)).fetchone()
            if owner and owner[0] != name:
                raise AirflowDatabaseError("Airflow database exists with an unexpected owner; restore its original configuration")
            if not owner:
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(name)))
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(name)))
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
            conn.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(sql.Identifier(name),sql.Identifier(name)))
            conn.execute("RESET ROLE")
            conn.execute("SELECT pg_advisory_unlock(%s)", (lock,))
    except psycopg.Error:
        # Database errors may include connection strings or SQL password literals.
        raise AirflowDatabaseError("cannot provision Airflow metadata; check shared database availability and provisioner privileges") from None
    keys['metadata_backend'] = 'shared-postgres'
    store.put(keys_path(pub_id), keys)
    return shared_database_url(pub_id, keys).render_as_string(hide_password=False)
