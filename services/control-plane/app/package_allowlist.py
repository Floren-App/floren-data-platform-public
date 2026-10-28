"""Which public packages may be imported from PyPI and CRAN (the package allowlist).

While enabled, an import (app, API or OpenTofu's fdp_package) may only request a package on the list; names end
in "*" for a family (e.g. "scikit-*"). Dependencies an allowed package needs come along with it, or it would not
install. Packages published here are not affected. Disabled (the default), Publishers import any package.
"""
from __future__ import annotations

import re
from typing import Dict, List

from packaging.utils import canonicalize_name
from sqlalchemy.orm import Session

from .models import PlatformSetting

KEY = "package_allowlist"
# An exact name, or a prefix followed by "*" (e.g. "scikit-*").
NAME = {"python": re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$|^[A-Za-z0-9][A-Za-z0-9._-]*\*$"),
        "r": re.compile(r"^[A-Za-z]([A-Za-z0-9.]*[A-Za-z0-9])?$|^[A-Za-z][A-Za-z0-9.]*\*$")}


def empty() -> dict:
    return {"enabled": False, "python": [], "r": []}


def get(db: Session) -> dict:
    row = db.get(PlatformSetting, KEY)
    return {**empty(), **((row.value or {}) if row else {})}


def stored(kind: str, name: str) -> str:
    """How a name is kept: Python's canonical form (PEP 503); R as written (its names keep their case, e.g. DBI)."""
    star = name.endswith("*")
    base = name[:-1] if star else name
    return (canonicalize_name(base) if kind == "python" else base) + ("*" if star else "")


def normalize(kind: str, name: str) -> str:
    """For comparing: canonical and case-insensitive."""
    return stored(kind, name).lower()


def save(db: Session, enabled: bool, entries: Dict[str, List[dict]]) -> dict:
    row = db.get(PlatformSetting, KEY) or PlatformSetting(key=KEY)
    row.value = {"enabled": enabled, **{kind: entries.get(kind, []) for kind in ("python", "r")}}
    db.add(row)
    return get(db)


def permits(db: Session, kind: str, name: str) -> bool:
    current = get(db)
    if not current["enabled"]:
        return True
    wanted = normalize(kind, name)
    for entry in current.get(kind, []):
        pattern = normalize(kind, entry["name"])
        if pattern.endswith("*") and wanted.startswith(pattern[:-1]) or wanted == pattern:
            return True
    return False
