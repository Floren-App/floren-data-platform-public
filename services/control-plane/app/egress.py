"""The workspace network allowlist: host names VS Code and RStudio may reach through the egress proxy.

Stored as a platform setting; the proxy (app/egress_proxy.py) reads it every few seconds, so changes apply to
running workspaces without restarts. Empty means no internet access from workspaces.
"""
from __future__ import annotations

import re
from typing import Dict, List

from sqlalchemy.orm import Session

from .models import PlatformSetting

KEY = "egress"
# "github.com" or "*.githubusercontent.com"; a wildcard needs a name and a top-level domain under it.
HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")


def valid_host(host: str) -> bool:
    return len(host) <= 253 and bool(HOST.fullmatch(host))


def hosts(db: Session, layer_id=None) -> List[Dict[str, str]]:
    row = db.get(PlatformSetting, KEY if layer_id is None else KEY + ":" + layer_id)
    return list((row.value or {}).get("hosts", [])) if row else []


def set_hosts(db: Session, entries: List[Dict[str, str]], layer_id=None) -> None:
    key = KEY if layer_id is None else KEY + ":" + layer_id
    row = db.get(PlatformSetting, key) or PlatformSetting(key=key)
    row.value = {"hosts": entries}
    db.add(row)


def permitted(db, host, layer_id):
    from .egress_proxy import allowed, package_index
    return bool(layer_id and not package_index(host) and
                allowed(host, [h["host"] for h in hosts(db)]) and
                allowed(host, [h["host"] for h in hosts(db, layer_id)]))


def within_ceiling(db, pattern):
    from .egress_proxy import allowed
    ceiling = [h["host"] for h in hosts(db)]
    if not pattern.startswith("*."):
        return allowed(pattern, ceiling)
    suffix = pattern[2:]
    return any(p.startswith("*.") and (suffix == p[2:] or suffix.endswith("." + p[2:])) for p in ceiling)
