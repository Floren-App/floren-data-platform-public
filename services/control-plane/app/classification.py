"""Classification: a configurable, ordered list of levels; columns carry them, objects inherit them.

A table's level is the highest of its columns' levels, unless the table itself is classified higher: an
object is never classified lower than its columns. Per request a masked column counts as its level's
"masked as" level, so masked access to personal data can count as, say, internal. A level set on the object
itself always counts, masked or not."""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy import select

from .models import CatalogAsset, PlatformSetting

SETTING = "classifications"
UNCLASSIFIED = "unclassified"
DEFAULT_LEVELS = [
    {"name": "public", "masked_as": "public", "description": "Anyone may see it."},
    {"name": "internal", "masked_as": "internal", "description": "For people in the organization."},
    {"name": "confidential", "masked_as": "internal", "description": "For people who need it for their work."},
    {"name": "restricted", "masked_as": "internal", "description": "Personal or sensitive data, under the strictest rules."},
]
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def levels(db) -> List[dict]:
    """Ordered from least to most sensitive."""
    setting = db.get(PlatformSetting, SETTING)
    return list((setting.value or {}).get("levels") or DEFAULT_LEVELS) if setting else list(DEFAULT_LEVELS)


def ranks(db) -> Dict[str, int]:
    out = {UNCLASSIFIED: -1}
    out.update({level["name"]: i for i, level in enumerate(levels(db))})
    return out


def require_level(db, name: str) -> str:
    if name not in ranks(db):
        raise HTTPException(422, f"unknown classification {name!r}; choose one of: " + ", ".join(l["name"] for l in levels(db)))
    return name


def highest(db, names: Iterable[Optional[str]]) -> str:
    order = ranks(db)
    best = UNCLASSIFIED
    for name in names:
        if name and order.get(name, -1) > order[best]:
            best = name
    return best


def masked_as(db, name: str) -> str:
    for level in levels(db):
        if level["name"] == name:
            return level.get("masked_as") or name
    return name


def column_governance(asset: CatalogAsset) -> Dict[str, dict]:
    return dict(((asset.governance or {}).get("columns") or {}))


def inherited(db, asset: CatalogAsset, masked: Iterable[str] = ()) -> str:
    """The highest level among the object's columns; masked columns count as their masked level."""
    hidden = {c.lower() for c in masked}
    return highest(db, (masked_as(db, g.get("classification")) if name.lower() in hidden else g.get("classification")
                        for name, g in column_governance(asset).items()))


def effective(db, asset: CatalogAsset, masked: Iterable[str] = ()) -> str:
    """What counts for this object when the given columns are masked: never below its own level."""
    return highest(db, [(asset.governance or {}).get("classification"), inherited(db, asset, masked)])


def describe(db, asset: CatalogAsset) -> dict:
    return {"explicit": (asset.governance or {}).get("classification", UNCLASSIFIED), "inherited": inherited(db, asset),
            "effective": effective(db, asset), "columns": column_governance(asset)}


def validate_levels(db, proposed: List[dict]) -> List[dict]:
    names = [l["name"] for l in proposed]
    if not proposed:
        raise HTTPException(422, "keep at least one classification")
    if len(set(names)) != len(names):
        raise HTTPException(422, "classification names must be unique")
    for i, level in enumerate(proposed):
        if not _NAME.fullmatch(level["name"]) or level["name"] == UNCLASSIFIED:
            raise HTTPException(422, f"{level['name']!r}: use lower-case letters, digits, - and _ (up to 32)")
        target = level.get("masked_as") or level["name"]
        if target not in names or names.index(target) > i:
            raise HTTPException(422, f"{level['name']}: masked data must count as itself or a lower level")
    removed = set(l["name"] for l in levels(db)) - set(names)
    if removed:
        for asset in db.scalars(select(CatalogAsset)):
            used = {(asset.governance or {}).get("classification")} | {g.get("classification") for g in column_governance(asset).values()}
            if used & removed:
                raise HTTPException(409, f"{', '.join(sorted(used & removed))} is still used, for example by {asset.name}")
    return [{"name": l["name"], "masked_as": l.get("masked_as") or l["name"], "description": l.get("description", "")} for l in proposed]
