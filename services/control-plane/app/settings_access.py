"""Every page in Access and Administration is an object you can grant, like a schema or a folder: Viewer may see
it, Manager may also change it. Sections are parents, so Manager on Privacy covers its pages and Manager on
Administration covers everything. Platform administrators (Global Admin) have all of it."""
from typing import Dict, Optional

from fastapi import Depends, HTTPException

from . import rbac
from .auth import User
from .db import get_db
from .deps import get_actor
from .models import Resource

ROOT = "admin-root"
SECTIONS: Dict[str, str] = {"adm-platform": "Platform", "adm-security": "Security", "adm-privacy": "Privacy", "adm-access": "Access"}
# page id (at most 12 characters, the resource ID) -> (name, section)
PAGES: Dict[str, tuple] = {
    "set-layers": ("Layers", "adm-platform"),
    "set-pools": ("Compute pools", "adm-platform"),
    "set-runtimes": ("Runtimes", "adm-platform"),
    "set-template": ("Workspace templates", "adm-platform"),
    "set-alerts": ("Alerts", "adm-platform"),
    "set-profiles": ("App profiles", "adm-security"),
    "set-network": ("Network allowlist", "adm-security"),
    "set-classes": ("Classifications", "adm-privacy"),
    "set-policies": ("Access policies", "adm-privacy"),
    "set-forms": ("Compliance forms", "adm-privacy"),
    "set-duties": ("Review duties", "adm-privacy"),
    "set-groups": ("Groups", "adm-access"),
    "set-services": ("Service identities", "adm-access"),
    "set-register": ("Access register", "adm-access"),
    "set-auditlog": ("Audit log", "adm-access"),
    "set-querylog": ("Query log", "adm-access"),
}
KINDS = ("settings-root", "settings-section", "setting")
ROLES = {"Viewer": {"read"}, "Manager": {"read", "manage"}}
ACTIONS = {"view": "read", "manage": "manage"}


def ensure(db) -> None:
    """The settings objects exist from the start (they are the app's own pages, not configuration)."""
    if db.get(Resource, ROOT) is None:
        db.add(Resource(id=ROOT, kind="settings-root", name="Administration"))
    for ident, name in SECTIONS.items():
        if db.get(Resource, ident) is None:
            db.add(Resource(id=ident, kind="settings-section", name=name, parent_id=ROOT))
    for ident, (name, section) in PAGES.items():
        if db.get(Resource, ident) is None:
            db.add(Resource(id=ident, kind="setting", name=name, parent_id=section))
    db.flush()


def allowed(db, user, page: str, action: str = "manage") -> bool:
    """action: view or manage."""
    resolved = rbac.resolve_user(db, user)
    if resolved.is_admin and not resolved.execution_context:
        return True
    if PAGES.get(page, ("", ""))[1] == "adm-privacy":
        from .compliance import has_role
        if has_role(db, resolved, "compliance_admin"):
            return True  # the earlier compliance administrator role keeps working
    resource = db.get(Resource, page)
    if resource is None:
        ensure(db)
        resource = db.get(Resource, page)
    return rbac.allowed(db, resolved, ACTIONS[action], resource)


def require(db, user, page: str, action: str = "manage"):
    if not allowed(db, user, page, action):
        raise HTTPException(403, f"{'Manager' if action == 'manage' else 'Viewer'} on {PAGES[page][0]} required")
    return rbac.resolve_user(db, user)


def needs(page: str, action: str = "manage"):
    """A route dependency: the caller needs this role on the page."""
    def check(db=Depends(get_db), user: User = Depends(get_actor)) -> User:
        return require(db, user, page, action)
    return check


def granted(db, user) -> Dict[str, Optional[str]]:
    """For the app: per page, "manage", "view" or nothing."""
    out = {}
    for page in PAGES:
        if allowed(db, user, page, "manage"):
            out[page] = "manage"
        elif allowed(db, user, page, "view"):
            out[page] = "view"
    return out
