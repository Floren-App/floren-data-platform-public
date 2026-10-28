"""profiles.yml for dbt, built from a Floren Data Platform data source (credentials never stored in the repo).

Inside runs, Floren Data Platform sets FDP_DBT_SOURCE/_TARGET/_SCHEMA/_THREADS. In a workspace terminal:

    fdp dbt-profile --source finance-dw --write   # ~/.dbt/profiles.yml, your dev schema
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

from . import credentials

_ADAPTERS = {
    "postgres": lambda cfg, cred: {"type": "postgres", "host": cfg.get("host"), "port": int(cfg.get("port", 5432)),
                                   "user": cred.get("username"), "password": cred.get("password"),
                                   "dbname": cfg.get("database")},
    "snowflake": lambda cfg, cred: {"type": "snowflake", "account": cfg.get("account"), "user": cred.get("username"),
                                    "password": cred.get("password"), "role": cfg.get("role"),
                                    "warehouse": cfg.get("warehouse"), "database": cfg.get("database")},
    "trino": lambda cfg, cred: {"type": "trino", "host": cfg.get("host"), "port": int(cfg.get("port", 443)),
                                "user": cred.get("username"), "database": cfg.get("catalog"), "method": "none",
                                "http_scheme": cfg.get("http_scheme", "https"),
                                "cert": True,
                                "http_headers": {"Authorization": "Bearer " + cred["token"]}},
}


def project_profile_name(project_dir: str = ".") -> str:
    """The `profile:` a dbt_project.yml asks for (profiles.yml must use that name)."""
    try:
        text = open(os.path.join(project_dir, "dbt_project.yml"), encoding="utf-8").read()
    except OSError:
        raise SystemExit("dbt_project.yml not found: run this inside the dbt project folder") from None
    m = re.search(r"^profile:\s*['\"]?([\w\-]+)", text, re.M)
    if not m:
        raise SystemExit("dbt_project.yml has no 'profile:' entry")
    return m.group(1)


def profile(source: str, schema: str, target: str = "dev", threads: int = 4,
            project_dir: str = ".", token: Optional[str] = None, info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    info = info or credentials(source, token=token, schema=schema)
    if info["kind"] == "trino":
        from .sql import endpoint
        parsed = endpoint(info)
        info = {**info, "config": {**info["config"], "host": parsed.hostname,
                "port": parsed.port or (443 if parsed.scheme == "https" else 80), "http_scheme": parsed.scheme}}
    if info["kind"] in ("iceberg", "s3"):
        raise SystemExit('dbt requires a SQL connection: use the governed Trino endpoint for Iceberg; '
                         'S3 file sources cannot be dbt targets')
    build = _ADAPTERS.get(info["kind"])
    if build is None:
        raise SystemExit(f"no dbt adapter mapping for {info['kind']!r} data sources")
    output = {k: v for k, v in build(info["config"], info["credentials"]).items() if v is not None}
    output.update(schema=schema, threads=threads)
    return {project_profile_name(project_dir): {"target": target, "outputs": {target: output}}}


def main(source: Optional[str], schema: Optional[str], target: Optional[str], threads: Optional[int],
         write: bool) -> None:
    source = source or os.environ.get("FDP_DBT_SOURCE")
    if not source:
        raise SystemExit("--source is required (the data source dbt builds in)")
    user = re.sub(r"[^a-z0-9]+", "_", os.environ.get("FDP_USER", "dev").lower())
    schema = schema or os.environ.get("FDP_DBT_SCHEMA") or f"dbt_{user}"
    target = target or os.environ.get("FDP_DBT_TARGET", "dev")
    threads = threads or int(os.environ.get("FDP_DBT_THREADS", "4"))
    info = credentials(source, schema=schema)
    # JSON is valid YAML, so no YAML library is needed.
    text = json.dumps(profile(source, schema, target, threads, info=info), indent=2) + "\n"
    if write:
        path = os.path.join(os.path.expanduser("~"), ".dbt", "profiles.yml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(text)
        print(f"wrote {path} (target {target}, schema {schema}); credentials are short-lived, re-run when they expire")
    else:
        print(text, end="")
