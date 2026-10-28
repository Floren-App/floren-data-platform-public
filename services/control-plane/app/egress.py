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


def hosts(db: Session) -> List[Dict[str, str]]:
    row = db.get(PlatformSetting, KEY)
    return list((row.value or {}).get("hosts", [])) if row else []


def set_hosts(db: Session, entries: List[Dict[str, str]]) -> None:
    row = db.get(PlatformSetting, KEY) or PlatformSetting(key=KEY)
    row.value = {"hosts": entries}
    db.add(row)
