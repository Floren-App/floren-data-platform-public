"""Management provenance, recorded with the audit event of each change.

The OpenTofu provider declares itself on every request (X-FDP-Managed-By: terraform, with an optional configuration
reference and revision). A change made under that declaration marks its target as Terraform-managed; a later change
without it is recorded as an app change, which the next Terraform plan will likely show as drift. Nothing is inferred
from user agents or directory origin, and objects never changed under a declaration keep unknown provenance.
"""
from contextvars import ContextVar
from typing import Optional

from .models import ManagementProvenance, utcnow

declared: ContextVar[Optional[dict]] = ContextVar("management_declaration", default=None)
HEADERS = {"source": "x-fdp-managed-by", "reference": "x-fdp-configuration", "revision": "x-fdp-configuration-revision"}


def from_headers(headers) -> Optional[dict]:
    if (headers.get(HEADERS["source"]) or "").strip().lower() != "terraform":
        return None
    return {"reference": (headers.get(HEADERS["reference"]) or "")[:512], "revision": (headers.get(HEADERS["revision"]) or "")[:128]}


def record(db, actor: str, action: str, target: str) -> None:
    """Called with each audited change. Session/identity bookkeeping (target '') is not configuration."""
    if not target:
        return
    declaration = declared.get()
    row = db.get(ManagementProvenance, target)
    if declaration is not None:
        if row is None:
            row = ManagementProvenance(target=target, source="terraform", managed_by=actor)
            db.add(row)
        row.source, row.reference, row.revision = "terraform", declaration["reference"], declaration["revision"]
        row.managed_by, row.managed_at = actor, utcnow()
        row.app_changed_at = row.app_changed_by = row.app_change = None
    elif row is not None and row.source == "terraform":
        row.app_changed_at, row.app_changed_by, row.app_change = utcnow(), actor, action[:64]


def output(row: Optional[ManagementProvenance]) -> dict:
    if row is None:
        return {"source": "unknown"}
    return {"source": row.source, "reference": row.reference, "revision": row.revision, "managed_by": row.managed_by,
            "managed_at": row.managed_at.isoformat(),
            "app_change": None if row.app_changed_at is None else
            {"at": row.app_changed_at.isoformat(), "by": row.app_changed_by, "action": row.app_change}}
