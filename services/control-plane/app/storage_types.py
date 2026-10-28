"""Storage integration types: the external locations Floren Data Platform can govern, and what each one requires.

An integration points at an external database/bucket or optional layer-local PostgreSQL.
SQL sources must also be configured as a catalog in the layer's Trino engine. The desktop renders
its forms from TYPES, so a field added here is validated server-side and shown in the UI.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import HTTPException

CATALOG = re.compile(r"[a-z][a-z0-9_]{0,62}")
BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")


def field(name, label, *, required=True, kind="text", placeholder="", help="", default=None, secret=False):
    return {"name": name, "label": label, "required": required, "kind": kind, "placeholder": placeholder,
            "help": help, "default": default, "secret": secret}


TYPES = {
    "postgres": {
        "label": "PostgreSQL",
        "summary": "A PostgreSQL database. Queries run through the shared SQL engine; schema roles are enforced there "
                   "and, after access setup, as native database roles.",
        "external": "Create a Trino catalog (connector postgresql) for this database with the same host and database, "
                    "using a read/write service login. Floren Data Platform references it by catalog name.",
        "config": [
            field("host", "Host", placeholder="warehouse.internal"),
            field("port", "Port", kind="port", default=5432),
            field("database", "Database", placeholder="finance"),
            field("catalog", "Trino catalog", kind="catalog", placeholder="finance",
                  help="Catalog name configured in the shared Trino engine for this database."),
            field("sslmode", "SSL mode", required=False, kind="choice", default="",
                  help="Used for access setup connections. Empty uses the driver default."),
            field("dynamic_role", "OpenBao database role", required=False, kind="text",
                  help="Native executor only: issue a fresh database login per session from this OpenBao role."),
        ],
        "secrets": [
            {"name": "provisioner_secret", "label": "Access-setup login", "required": True,
             "help": "A database role that may create roles and grant schema privileges. Used only by the control "
                     "plane to set up access; executions never receive it.",
             "fields": [field("username", "Username", placeholder="governance_admin"),
                        field("password", "Password", kind="password")]},
            {"name": "secret", "label": "Engine login", "required": False,
             "help": "The read/write service login the layer's Trino uses for this database. In a managed layer "
                     "the platform writes the Trino catalog from it; otherwise configure the catalog yourself.",
             "fields": [field("username", "Username"), field("password", "Password", kind="password")]},
        ],
    },
    "iceberg": {
        "label": "Iceberg tables on S3",
        "summary": "Iceberg tables in an S3-compatible bucket, served by a REST catalog. Queries run through the "
                   "shared SQL engine; table storage credentials stay in that engine.",
        "external": "Create a Trino catalog (connector iceberg, REST catalog type) pointing at the REST catalog and "
                    "warehouse below, with its own S3 credentials. The warehouse bucket is reserved for tables.",
        "config": [
            field("catalog", "Trino catalog", kind="catalog", placeholder="lake",
                  help="Catalog name configured in the shared Trino engine for these tables."),
            field("warehouse", "Warehouse location", kind="s3uri", placeholder="s3://lake/",
                  help="Where table data lives. This bucket cannot also be registered for plain files."),
            field("catalog_uri", "REST catalog URL", kind="url", placeholder="http://iceberg-rest:8181"),
            field("endpoint_url", "S3 endpoint", required=False, kind="url", placeholder="https://s3.eu-west-1.amazonaws.com",
                  help="Empty for AWS S3."),
            field("region", "Region", default="us-east-1"),
        ],
        "secrets": [
            {"name": "secret", "label": "Engine storage login", "required": False,
             "help": "S3 credentials the layer's Trino uses for the table data. In a managed layer the platform "
                     "writes the Trino catalog from them; otherwise configure the catalog yourself.",
             "fields": [field("access_key_id", "Access key ID"), field("secret_access_key", "Secret access key", kind="password")]},
        ],
    },
    "s3": {
        "label": "S3 files",
        "summary": "Plain files under a prefix of an S3-compatible bucket. Readers and contributors receive short-lived "
                   "credentials scoped to their namespaces' prefixes.",
        "external": "Create a role that can access the bucket and that the provisioning login may assume (STS "
                    "AssumeRole). Floren Data Platform narrows each session to the granted prefixes.",
        "config": [
            field("bucket", "Bucket", kind="bucket", placeholder="files"),
            field("prefix", "Base prefix", required=False, placeholder="finance",
                  help="Namespaces are registered below this prefix. Empty uses the whole bucket."),
            field("region", "Region", default="us-east-1"),
            field("endpoint_url", "S3 endpoint", required=False, kind="url", placeholder="https://s3.eu-west-1.amazonaws.com",
                  help="Empty for AWS S3."),
            field("sts_endpoint_url", "STS endpoint", required=False, kind="url", help="Empty for AWS STS."),
            field("role_arn", "Role to assume", kind="arn", placeholder="arn:aws:iam::123456789012:role/fdp-files"),
        ],
        "secrets": [
            {"name": "provisioner_secret", "label": "STS provisioning login", "required": True,
             "help": "Access key allowed to assume the role. Stays in the control plane; executions never receive it.",
             "fields": [field("access_key_id", "Access key ID"), field("secret_access_key", "Secret access key", kind="password")]},
        ],
    },
}


def bucket_of(uri: str) -> Optional[str]:
    match = re.fullmatch(r"s3a?://([^/]+)(/.*)?", uri or "")
    return match.group(1) if match else None


def _check(f: dict, value) -> Optional[str]:
    kind = f["kind"]
    if kind == "port":
        return None if isinstance(value, int) and 0 < value < 65536 else "enter a port between 1 and 65535"
    if not isinstance(value, str):
        return "must be text"
    if kind == "catalog" and not CATALOG.fullmatch(value):
        return "lowercase letters, digits and underscores, starting with a letter"
    if kind == "catalog" and value == "system":
        return "the system catalog cannot be registered"
    if kind == "bucket" and not BUCKET.fullmatch(value):
        return "enter a literal bucket name"
    if kind == "s3uri" and not bucket_of(value):
        return "enter an s3://bucket/path location"
    if kind == "url" and not re.fullmatch(r"https?://[^\s/]+(/\S*)?", value):
        return "enter an http(s) URL"
    if kind == "arn" and not value.startswith("arn:"):
        return "enter a role ARN"
    if kind == "choice" and value not in ("", "disable", "allow", "prefer", "require", "verify-ca", "verify-full"):
        return "choose a PostgreSQL sslmode"
    return None


def validate(kind: str, config: dict, secrets: dict, has_stored: dict) -> dict:
    """Returns the normalized config; raises 422 with per-field errors. has_stored: secret name -> already stored."""
    spec = TYPES.get(kind)
    if not spec:
        raise HTTPException(422, f"choose one of: {', '.join(TYPES)}")
    errors, clean = {}, {}
    for f in spec["config"]:
        value = config.get(f["name"])
        if value in (None, "") and f["default"] is not None:
            value = f["default"]
        if f["kind"] == "port" and isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, str):
            value = value.strip()
        if value in (None, ""):
            if f["required"]:
                errors[f["name"]] = "required"
            continue
        problem = _check(f, value)
        if problem:
            errors[f["name"]] = problem
        else:
            clean[f["name"]] = value
    for group in spec["secrets"]:
        given = secrets.get(group["name"])
        if given is None:
            if group["required"] and not has_stored.get(group["name"]):
                for f in group["fields"]:
                    errors[f"{group['name']}.{f['name']}"] = "required"
            continue
        for f in group["fields"]:
            if not str(given.get(f["name"], "")).strip():
                errors[f"{group['name']}.{f['name']}"] = "required"
    if errors:
        raise HTTPException(422, {"message": f"{spec['label']} integration is incomplete", "errors": errors})
    return clean


def lake_buckets(db) -> set:
    """Buckets holding Iceberg tables: reachable only through governed SQL, never as plain files."""
    from sqlalchemy import select
    from .config import get_settings
    from .models import DataSource
    reserved = {b.strip() for b in get_settings().sql_lake_buckets.split(",") if b.strip()}
    return reserved | {bucket_of(ds.config.get("warehouse", "")) for ds in db.scalars(select(DataSource).where(DataSource.kind == "iceberg"))} - {None}


def public_types() -> list:
    return [{"kind": kind, **{k: v for k, v in spec.items()}} for kind, spec in TYPES.items()]
