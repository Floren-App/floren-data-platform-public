"""One page gate for every desktop tab and legacy alias."""
from . import permissions, rbac
from .app_profiles import acting_as_admin

# Stable page IDs. A page grant has namespace "pages", target_id equal to this ID.
PAGES = {
    "home": "Home", "catalog": "Catalog", "products": "Data products", "requests": "Access requests",
    "people": "People", "set-groups": "Groups", "workspaces": "Workspaces", "runs": "Runs",
    "storage": "Storage", "kafka": "Kafka", "usage": "Usage", "workloads": "Workloads", "apis": "APIs",
    "set-services": "Service identities", "set-layers": "Layers", "set-profiles": "App profiles",
    "set-alerts": "Alerts", "set-classes": "Classifications", "set-policies": "Access policies",
    "reviews": "Reviews", "memberships": "Membership approvals", "set-register": "Access register",
    "cases": "Compliance cases", "set-forms": "Compliance forms", "set-duties": "Review duties",
    "repos": "Repositories", "set-runtimes": "Runtimes", "secrets": "Secrets", "docs": "Workload documentation",
    "set-pools": "Compute pools", "set-template": "Workspace templates", "set-network": "Network policies",
    "set-querylog": "Query log", "git-creds": "Git credentials", "packages": "Packages",
    "imports": "Package imports", "set-auditlog": "Audit log", "deadlines": "Deadlines",
}
FEATURE_PAGES = {
    "products.browse": "products", "products.request": "requests", "products.publish": "products",
    "catalog.objects": "catalog", "workspaces": "workspaces", "workloads.view": "workloads",
    "workloads.manage": "workloads", "storage": "storage", "packages": "packages", "kafka": "kafka",
    "apis": "apis", "usage": "usage", "security": "set-groups",
}


def allowed(db, user, page, layer=None):
    if page not in PAGES:
        return False
    user = rbac.resolve_user(db, user)
    if acting_as_admin(user):
        return True
    if permissions.allowed(db, user, "pages.view", page) or permissions.allowed(db, user, "pages.view", page, layer):
        return True
    from .models import Resource
    # Existing explicit page assignments remain page grants, never implicit feature access.
    resource = db.get(Resource, page)
    if resource and resource.kind in ("setting", "settings-section", "settings-root"):
        return any(not grant.get("system") and grant["role"] in permissions.ROLES
                   for grant in rbac.access(db, user, resource))
    return False


def granted(db, user, layer=None):
    return {page: True for page in PAGES if allowed(db, user, page, layer)}
