"""App profiles: which parts of the app a group gets.

Object permissions say what someone may open; a profile says which features a group gets at all: pages,
navigation and the API actions behind them. A person gets the profile of their active group (switching group
switches the app). Global Admin always has everything. Profiles apply to people using the app; running
programs, workspaces and service identities are governed by their object permissions only."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

from fastapi import Header, HTTPException, Request
from sqlalchemy import select

from .models import AppGroup, AppProfile

FEATURES: Dict[str, str] = {
    "products.browse": "Data products: browse them and open the dashboards you can use",
    "products.request": "Request access to data products",
    "products.publish": "Publish data products and manage their access",
    "catalog.objects": "All objects in the catalog, with lineage and governance",
    "workspaces": "Workspaces, workspace runs and Git credentials",
    "workloads.view": "Workloads: browse them, run on demand, read their logs",
    "workloads.manage": "Workloads: deploy, change and organize them",
    "storage": "Storage: browse data and query it with SQL",
    "packages": "Packages",
    "kafka": "Kafka",
    "apis": "API management",
    "usage": "Compute usage",
    "security": "Security: manage app groups, their members and services",
}
# A feature that needs another to be useful brings it along.
IMPLIES = {"products.request": {"products.browse"}, "products.publish": {"products.browse"},
           "workloads.manage": {"workloads.view"}}
DEFAULT_PROFILES = [
    ("Full access", "Everything the platform offers; object permissions still decide what can be opened.", list(FEATURES), True),
    ("Engineer", "Build, deploy and share: everything except managing groups and services.",
     [f for f in FEATURES if f != "security"], False),
    ("Analyst", "Find and request data, work in workspaces and SQL, run workloads.",
     ["products.browse", "products.request", "catalog.objects", "workspaces", "workloads.view", "storage"], False),
    ("Viewer", "Find data products and open the dashboards you can use.", ["products.browse"], False),
]


def normalize(features: Iterable[str]) -> List[str]:
    """Known features only, plus what they need, in the catalog's order."""
    chosen = set(features)
    unknown = chosen - set(FEATURES)
    if unknown:
        raise HTTPException(422, "unknown app features: " + ", ".join(sorted(unknown)))
    for feature in list(chosen):
        chosen |= IMPLIES.get(feature, set())
    return [f for f in FEATURES if f in chosen]


def ensure_defaults(db) -> None:
    """The built-in profiles, created once; afterwards they are configuration like any other."""
    if db.scalar(select(AppProfile.id).limit(1)):
        return
    for name, description, features, default in DEFAULT_PROFILES:
        db.add(AppProfile(name=name, description=description, features=normalize(features), default=default))
    db.flush()


def default_profile(db) -> Optional[AppProfile]:
    ensure_defaults(db)
    return db.scalar(select(AppProfile).where(AppProfile.default.is_(True))) or db.scalar(select(AppProfile).order_by(AppProfile.name))


def profile_for(db, user) -> Optional[AppProfile]:
    group = db.get(AppGroup, user.active_group_id) if getattr(user, "active_group_id", None) else None
    profile = db.get(AppProfile, group.profile_id) if group is not None and group.profile_id else None
    return profile or default_profile(db)


def interactive(user) -> bool:
    return not user.execution_context and not user.username.startswith(("service:", "sa:"))


def features_for(db, user) -> Set[str]:
    if user.is_admin and not user.execution_context:
        return set(FEATURES)
    if not interactive(user):
        return set(FEATURES)  # programs are limited by object permissions, not by app profiles
    profile = profile_for(db, user)
    return set(profile.features) if profile else set(FEATURES)


def required_feature(method: str, path: str) -> Optional[str]:
    """The feature an API route belongs to (by its route template), or None when only object permissions apply."""
    m = method.upper()
    if path.startswith("/v1/me/git-credentials") or path.startswith("/v1/workspaces"):
        return "workspaces"
    if path == "/v1/runs" and m == "POST":
        return "workspaces"
    if path.startswith("/v1/workloads"):
        if path.endswith("/open"):
            return "products.browse"  # opening a dashboard you may use
        if m == "GET" or path.endswith("/runs"):
            return "workloads.view"
        return "workloads.manage"
    if path.startswith("/v1/repos"):
        return "workloads.manage"
    if path.startswith(("/v1/data/", "/v1/sql/")):
        return "storage"
    if path.startswith("/v1/packages"):
        return "packages"
    if path.startswith("/v1/kafka"):
        return "kafka"
    if path.startswith("/v1/apis"):
        return "apis"
    if path.startswith("/v1/compute"):
        return "usage"
    if path.startswith("/v1/logs") and m == "GET":
        return "workloads.view"
    if path.startswith("/v1/catalog/products"):
        if path.endswith("/requests") and m == "POST":
            return "products.request"
        return "products.browse" if m == "GET" else "products.publish"
    if path.startswith("/v1/catalog/assets/{ident}/product"):
        return "products.browse"
    if path.startswith(("/v1/catalog/requests", "/v1/catalog/ingest")):
        return None  # owners and requesters only see their own; programs ingest lineage
    if path.startswith("/v1/catalog"):
        return "catalog.objects"
    if path.startswith("/v1/rbac/groups") and m != "GET":
        return "security"
    if path.startswith("/v1/rbac/directory"):
        return "security"
    return None


def feature_gate(request: Request, authorization: Optional[str] = Header(default=None),
                 x_fdp_active_group: Optional[str] = Header(default=None)) -> None:
    """Refuses API actions of features the caller's app profile does not include."""
    route = request.scope.get("route")
    feature = required_feature(request.method, getattr(route, "path", request.url.path))
    if feature is None or not authorization:
        return
    from .deps import get_actor
    try:
        user = get_actor(authorization, x_fdp_active_group)
    except HTTPException:
        return  # the route itself refuses unauthenticated calls
    if user.is_admin or not interactive(user):
        return
    from .db import SessionLocal
    with SessionLocal() as db:
        allowed = features_for(db, user)
        db.commit()
    if feature not in allowed:
        raise HTTPException(403, f"your app profile does not include: {FEATURES[feature]}")
