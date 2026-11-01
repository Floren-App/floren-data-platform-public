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
# Viewer and Manager are given to groups; Owner is Global Admin's protected access to every object, so platform
# administrators see these pages in listings like any other object.
ROLES = {"Viewer": {"read"}, "Executor": {"read"}, "Contributor": {"read"},
         "Manager": {"read", "manage"}, "Admin": {"read", "manage"}, "Owner": {"read", "manage", "assign"}}
ACTIONS = {"view": "read", "manage": "manage"}
NAMESPACES = {
    "set-layers": "layers.object", "set-pools": "compute.pools", "set-runtimes": "runtimes",
    "set-template": "workspace_templates", "set-alerts": "alerts.rules", "set-profiles": "profiles",
    "set-network": "network.editor_allowlist", "set-classes": "classification.dictionary",
    "set-policies": "compliance.policies", "set-forms": "compliance.forms", "set-duties": "compliance.duties",
    "set-groups": "groups", "set-services": "identities", "set-register": "compliance.register",
    "set-auditlog": "audit", "set-querylog": "query_log",
}


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


def allowed(db, user, page: str, action: str = "manage", layer_id=None) -> bool:
    """action: view or manage."""
    resolved = rbac.resolve_user(db, user)
    from .app_profiles import acting_as_admin
    from . import page_access, permissions
    if acting_as_admin(resolved):
        return True
    from .app_profiles import interactive
    if interactive(resolved) and not page_access.allowed(db, resolved, page, layer_id):
        return False
    if action == "view":
        return True
    if action not in ACTIONS:
        if permissions.allowed(db, resolved, action, page, layer_id):
            return True
        definition = permissions.REGISTRY.get(action)
        if not definition:
            return False
        # Explicit legacy page roles remain broad until their assignments are migrated.
        # Scoped grants never fall back to an unscoped legacy manager.
        if layer_id is not None:
            return False
        legacy = "read" if "Viewer" in definition["roles"] else "assign" if definition["roles"] == ["Owner"] else "manage"
        return rbac.allowed(db, resolved, legacy, db.get(Resource, page))
    resource = db.get(Resource, page)
    if resource is None:
        ensure(db)
        resource = db.get(Resource, page)
    return rbac.allowed(db, resolved, ACTIONS[action], resource)


def require(db, user, page: str, action: str = "manage", layer_id=None):
    if not allowed(db, user, page, action, layer_id):
        label = 'Manager' if action == 'manage' else 'Viewer' if action == 'view' else action
        raise HTTPException(403, f"{label} on {PAGES[page][0]} required")
    return rbac.resolve_user(db, user)


def needs(page: str, action: str = "manage"):
    """A route dependency: the caller needs this role on the page."""
    def check(db=Depends(get_db), user: User = Depends(get_actor)) -> User:
        return require(db, user, page, action)
    return check


def granted(db, user, layer_id=None) -> Dict[str, Optional[str]]:
    """For the app: per page, "manage", "view" or nothing. Platform administrators with Global Admin active get every
    page; with another group active they use explicit grants, including in backend authorization."""
    from .app_profiles import acting_as_admin
    resolved = rbac.resolve_user(db, user)
    if acting_as_admin(resolved):
        return {page: "manage" for page in PAGES}
    out = {}
    for page, (_, section) in PAGES.items():
        if allowed(db, resolved, page, "manage", layer_id):
            out[page] = "manage"
            continue
        if allowed(db, resolved, page, "view", layer_id):
            out[page] = "view"
    return out
