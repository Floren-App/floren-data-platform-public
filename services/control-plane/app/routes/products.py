"""Data products: catalog objects their owners published for others to find, understand and request.

A product's card (name, summary, owner, domain, classification, freshness) is visible to everyone in its
layer, with or without access. Its lineage from the sources shows the steps by name, kind and owner only;
details (columns, preview, SQL) stay behind the object's own permissions. Owners approve access requests,
which grant the product's reader role to the group the requester chose."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import catalog as c, layers, rbac
from ..auth import User
from ..db import get_db
from ..deps import get_actor
from ..models import (AccessRequest, AppGroup, CatalogAsset, CatalogProduct, LineageEdge, Resource, RoleAssignment,
                      Workload, utcnow)
from .common import audit

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])

PUBLISHABLE = {"table", "view", "materialized_view", "app", "api", "topic"}
# What an approved request grants on the object's resource: the role that lets someone read or use it.
READER_ROLE = {"schema": "Reader", "source": "Reader", "topic": "Reader", "kafka": "Reader", "service": "Executor",
               "api": "Consumer", "api-group": "Consumer", "api-operation": "Consumer"}
DATA_RELATIONS = ("reads", "writes", "depends_on", "uses")
PATH_DEPTH, PATH_LIMIT = 6, 40


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(min_length=1, max_length=32)
    title: str = Field(default="", max_length=200)
    summary: str = Field(default="", max_length=600)
    domain: str = Field(default="", max_length=80)
    classification: Optional[Literal["public", "internal", "confidential", "restricted"]] = None
    also: List[str] = Field(default_factory=list, max_length=20, description="Objects it uses to publish as well")


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    summary: Optional[str] = Field(default=None, max_length=600)


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_group_id: str = Field(min_length=1, max_length=12)
    reason: str = Field(min_length=3, max_length=1000)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: str = Field(default="", max_length=1000)


def _live(db: Session, resource: Optional[Resource]) -> bool:
    if resource is None or (resource.config or {}).get("deleted"):
        return False
    if resource.kind == "service":
        pub = db.get(Workload, resource.object_id)
        return bool(pub) and pub.status != "stopped"
    return True


def _in_layer(db, user, resource) -> bool:
    from ..environments import permits
    return permits(db, user, resource)


def _owner(db, asset, resource):
    group_id = (asset.governance or {}).get("owner_group_id")
    if not group_id:
        from ..ownership import owner_info
        group_id = owner_info(db, resource).get("owner_group_id")
    group = db.get(AppGroup, group_id) if group_id else None
    return group.name if group else None


def _freshness(db, asset, resource) -> Optional[datetime]:
    if resource.kind == "service":
        pub = db.get(Workload, resource.object_id)
        return pub.deployed_at if pub else None
    return asset.last_success_at or asset.observed_at


def _runtime(db, resource):
    if resource.kind == "service":
        pub = db.get(Workload, resource.object_id)
        return pub.runtime if pub else None
    return None


class _Viewer:
    """Per-request permission snapshot of the viewer."""

    def __init__(self, db: Session, user: User):
        self.db, self.user = db, rbac.resolve_user(db, user)
        _, self.actions = c.permissions(db, self.user)
        self.groups = rbac.app_groups(db, self.user)

    def can(self, resource_id: str, action: str) -> bool:
        return action in self.actions.get(resource_id, set())


def card(db: Session, product: CatalogProduct, viewer: _Viewer) -> dict:
    asset = db.get(CatalogAsset, product.asset_id)
    resource = db.get(Resource, product.resource_id)
    governance = asset.governance or {}
    pending = db.scalar(select(AccessRequest).where(AccessRequest.product_id == product.id, AccessRequest.status == "pending",
                                                    AccessRequest.requester_principal_id == (viewer.user.principal_id or ""),
                                                    AccessRequest.requester == viewer.user.username))
    granted = viewer.can(resource.id, "read")
    level = c.service_level(db, asset, resource)
    return {"id": product.id, "asset_id": asset.id, "title": product.title, "summary": product.summary,
            "kind": asset.kind, "runtime": _runtime(db, resource), "name": asset.name,
            **layers.info(db, resource),
            "domain": governance.get("domain") or "", "classification": governance.get("classification", "unclassified"),
            "status": governance.get("status", "draft"), "steward": governance.get("steward") or "",
            "owner_group_name": _owner(db, asset, resource), "updated_at": _freshness(db, asset, resource),
            "sla_status": level.get("status"), "published_at": product.published_at, "published_by": product.published_by,
            "access": "granted" if granted else "requested" if pending else "none",
            "can_manage": viewer.can(resource.id, "assign"),
            "grants": READER_ROLE.get(resource.kind, "Reader"),
            "grants_on": {"kind": resource.kind, "name": resource.name},
            # Where to open the object itself: only for people who may.
            "open": {"resource_kind": resource.kind, "resource_id": resource.id, "object_id": resource.object_id,
                     "object_path": (asset.attributes or {}).get("object_path")} if granted else None}


def _visible_products(db: Session, viewer: _Viewer, layer_id: str = ""):
    stmt = select(CatalogProduct)
    if layer_id:
        stmt = stmt.where(CatalogProduct.layer_id == layer_id)
    out = []
    for product in db.scalars(stmt.order_by(CatalogProduct.title)):
        resource = db.get(Resource, product.resource_id)
        if _live(db, resource) and _in_layer(db, viewer.user, resource):
            out.append(product)
    return out


@router.get("/products")
def list_products(layer_id: str = "", q: str = Query(default="", max_length=200), domain: str = "", kind: str = "",
                  accessible: bool = False, mine: bool = False,
                  db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Published data products in a layer, for everyone in it. accessible: only ones you can use;
    mine: only ones you manage."""
    viewer = _Viewer(db, user)
    cards = [card(db, p, viewer) for p in _visible_products(db, viewer, layer_id)]
    terms = q.casefold().split()
    cards = [x for x in cards if (not domain or x["domain"] == domain) and (not kind or x["kind"] == kind)
             and (not accessible or x["access"] == "granted") and (not mine or x["can_manage"])
             and all(t in " ".join(str(x[k] or "") for k in ("title", "summary", "domain", "owner_group_name", "name", "kind")).casefold()
                     for t in terms)]
    return {"items": cards, "domains": sorted({x["domain"] for x in cards if x["domain"]}),
            "kinds": sorted({x["kind"] for x in cards})}


@router.get("/products/candidates")
def publish_candidates(layer_id: str = "", q: str = Query(default="", max_length=200),
                       db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Objects you may publish: tables and views, dashboards, APIs and topics you own, not yet published."""
    viewer = _Viewer(db, user)
    c.sync_registry(db)
    published = set(db.scalars(select(CatalogProduct.asset_id)))
    out = []
    for asset in db.scalars(select(CatalogAsset).where(CatalogAsset.kind.in_(PUBLISHABLE))):
        resource = db.get(Resource, asset.resource_id)
        if asset.id in published or not _live(db, resource) or not viewer.can(resource.id, "assign"):
            continue
        where = layers.info(db, resource)
        if layer_id and where["layer_id"] not in (layer_id, None):
            continue
        if q and q.casefold() not in (asset.name + " " + asset.path).casefold():
            continue
        out.append({"asset_id": asset.id, "name": asset.name, "kind": asset.kind, "path": asset.path, **where})
    db.commit()
    return sorted(out, key=lambda a: (a["kind"], a["name"].casefold()))[:200]


def _upstream(db: Session, asset_id: str, depth: int = 1) -> Dict[str, int]:
    seen, frontier = {asset_id: 0}, {asset_id}
    for level in range(1, depth + 1):
        found = set()
        for e in db.scalars(select(LineageEdge).where(LineageEdge.target_id.in_(frontier), LineageEdge.relation.in_(DATA_RELATIONS))):
            if e.source_id not in seen:
                seen[e.source_id] = level
                found.add(e.source_id)
        frontier = found
        if not frontier or len(seen) > PATH_LIMIT * 3:
            break
    return seen


@router.get("/assets/{ident}/product")
def asset_product(ident: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Whether an object is published, and what it uses that you could publish with it."""
    asset, resource = c.require_asset(db, ident, user)
    viewer = _Viewer(db, user)
    product = db.scalar(select(CatalogProduct).where(CatalogProduct.asset_id == asset.id))
    published = set(db.scalars(select(CatalogProduct.asset_id)))
    uses = []
    if asset.kind in PUBLISHABLE:
        for other_id, level in _upstream(db, asset.id, 1).items():  # what it reads directly
            other = db.get(CatalogAsset, other_id)
            if not other or other.id == asset.id or other.kind not in PUBLISHABLE - {"app"}:
                continue
            res = db.get(Resource, other.resource_id)
            if _live(db, res) and viewer.can(res.id, "assign") and layers.info(db, res)["layer_id"] == layers.info(db, resource)["layer_id"]:
                uses.append({"asset_id": other.id, "name": other.name, "kind": other.kind, "published": other.id in published})
    return {"publishable": asset.kind in PUBLISHABLE, "can_publish": viewer.can(resource.id, "assign"),
            "product": card(db, product, viewer) if product else None, "uses": uses,
            "domain": (asset.governance or {}).get("domain") or "",
            "classification": (asset.governance or {}).get("classification", "unclassified")}


def _publish_one(db, viewer, asset_id, title="", summary="", domain="", classification=None) -> CatalogProduct:
    asset = db.get(CatalogAsset, asset_id)
    resource = db.get(Resource, asset.resource_id) if asset else None
    if not asset or not _live(db, resource) or not viewer.can(resource.id, "read"):
        raise HTTPException(404, "catalog object not available")
    if asset.kind not in PUBLISHABLE:
        raise HTTPException(422, "publish tables and views, dashboards, APIs or Kafka topics")
    if not viewer.can(resource.id, "assign"):
        raise HTTPException(403, f"only the owners of {asset.name} can publish it")
    existing = db.scalar(select(CatalogProduct).where(CatalogProduct.asset_id == asset.id))
    if existing:
        return existing
    governance = dict(asset.governance or {})
    if domain and not governance.get("domain"):
        governance["domain"] = domain
    if classification and governance.get("classification", "unclassified") == "unclassified":
        governance["classification"] = classification
    if governance != (asset.governance or {}):
        asset.governance = governance
        c.reindex(asset)
    product = CatalogProduct(asset_id=asset.id, resource_id=resource.id, layer_id=layers.info(db, resource)["layer_id"],
                             title=(title.strip() or asset.name)[:200], summary=summary.strip(),
                             published_by=viewer.user.username)
    db.add(product)
    db.flush()
    audit(db, viewer.user.username, "catalog.publish", resource.id, asset=asset.id, product=product.id, title=product.title)
    return product


@router.post("/products", status_code=201)
def publish(body: PublishIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    viewer = _Viewer(db, user)
    product = _publish_one(db, viewer, body.asset_id, body.title, body.summary, body.domain, body.classification)
    for other in body.also:
        _publish_one(db, viewer, other, domain=body.domain, classification=body.classification)
    db.commit()
    return card(db, product, viewer)


def _product(db, product_id, viewer) -> CatalogProduct:
    product = db.get(CatalogProduct, product_id)
    resource = db.get(Resource, product.resource_id) if product else None
    if not product or not _live(db, resource) or not _in_layer(db, viewer.user, resource):
        raise HTTPException(404, "data product not found")
    return product


@router.patch("/products/{product_id}")
def update_product(product_id: str, body: ProductPatch, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    if not viewer.can(product.resource_id, "assign"):
        raise HTTPException(403, "only the product's owners can change it")
    if body.title is not None:
        product.title = body.title.strip()
    if body.summary is not None:
        product.summary = body.summary.strip()
    product.updated_at = utcnow()
    audit(db, viewer.user.username, "catalog.product_update", product.resource_id, product=product.id)
    db.commit()
    return card(db, product, viewer)


@router.delete("/products/{product_id}", status_code=204)
def unpublish(product_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    if not viewer.can(product.resource_id, "assign"):
        raise HTTPException(403, "only the product's owners can unpublish it")
    for request in db.scalars(select(AccessRequest).where(AccessRequest.product_id == product.id, AccessRequest.status == "pending")):
        request.status, request.decided_by, request.decided_at = "withdrawn", viewer.user.username, utcnow()
        request.decision_note = "The product was unpublished."
    audit(db, viewer.user.username, "catalog.unpublish", product.resource_id, product=product.id)
    db.delete(product)
    db.commit()


def _node(db, asset, resource, viewer, products, depth):
    visible = viewer.can(resource.id, "read")
    out = {"asset_id": asset.id if visible else None, "name": asset.name, "kind": asset.kind, "depth": depth,
           "owner_group_name": _owner(db, asset, resource), "visible": visible,
           "product_id": products.get(asset.id)}
    if visible:
        out["runtime"] = _runtime(db, resource)
    return out


@router.get("/products/{product_id}")
def product_detail(product_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """The card, its upstream lineage (from the sources), and the products built on it.
    Steps you may not open show only their name, kind and owner."""
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    asset = db.get(CatalogAsset, product.asset_id)
    layer = product.layer_id
    products = dict(db.execute(select(CatalogProduct.asset_id, CatalogProduct.id)).all())
    # Upstream: follow data relations backwards; stay in the product's layer; skip operational objects.
    seen = _upstream(db, asset.id, PATH_DEPTH)
    path, real = [], []  # real: the asset id behind each path entry (never sent for steps you cannot open)
    kept = {asset.id}
    for ident, depth in sorted(seen.items(), key=lambda item: item[1]):
        if ident == asset.id or len(path) >= PATH_LIMIT:
            continue
        node = db.get(CatalogAsset, ident)
        resource = db.get(Resource, node.resource_id) if node else None
        if not node or not _live(db, resource) or layers.info(db, resource)["layer_id"] not in (layer, None):
            continue
        if (node.governance or {}).get("lineage_role") == "operational" or (node.attributes or {}).get("inactive"):
            continue
        if node.kind == "file":  # files are storage detail: show the bucket or prefix they live in
            bucket = db.get(CatalogAsset, c.asset_id(resource.id))
            if bucket is None or bucket.id in kept:
                continue
            node = bucket
        kept.add(node.id)
        path.append(_node(db, node, resource, viewer, products, depth))
        real.append(node.id)
    # Steps are keyed by position, so hidden steps connect without exposing their ids.
    key_of = {ident: f"step-{i}" for i, ident in enumerate(real)}
    key_of[asset.id] = "product"
    for i, node in enumerate(path):
        node["key"] = f"step-{i}"
    edges = sorted({(key_of[e.source_id], key_of[e.target_id]) for e in db.scalars(select(LineageEdge).where(
        LineageEdge.target_id.in_(kept), LineageEdge.source_id.in_(kept), LineageEdge.relation.in_(DATA_RELATIONS)))
        if e.source_id in key_of and e.target_id in key_of and e.source_id != e.target_id})
    # Downstream: products built on this one (named for everyone), and how many other objects use it.
    downstream, frontier, others = {}, {asset.id}, 0
    for _ in range(4):
        found = set()
        for e in db.scalars(select(LineageEdge).where(LineageEdge.source_id.in_(frontier), LineageEdge.relation.in_(DATA_RELATIONS))):
            if e.target_id not in downstream and e.target_id != asset.id:
                downstream[e.target_id] = True
                found.add(e.target_id)
        frontier = found
        if not frontier:
            break
    used_by = []
    for ident in downstream:
        if ident in products:
            other = db.get(CatalogProduct, products[ident])
            resource = db.get(Resource, other.resource_id)
            if other.layer_id == layer and _live(db, resource):
                used_by.append(card(db, other, viewer))
        else:
            node = db.get(CatalogAsset, ident)
            resource = db.get(Resource, node.resource_id) if node else None
            if node and _live(db, resource) and node.kind != "file":
                others += 1
    out = card(db, product, viewer)
    out.update({"lineage": path, "used_by": used_by, "used_by_others": others,
                "lineage_edges": [{"source": a, "target": b} for a, b in edges]})
    db.commit()
    return out


# --- access requests ---------------------------------------------------------------------------------------

def request_out(db, r: AccessRequest) -> dict:
    product = db.get(CatalogProduct, r.product_id)
    group = db.get(AppGroup, r.app_group_id)
    return {"id": r.id, "product_id": r.product_id, "product": product.title if product else "", "requester": r.requester,
            "app_group_id": r.app_group_id, "group": group.name if group else r.app_group_id, "reason": r.reason,
            "status": r.status, "role": r.role, "decided_by": r.decided_by, "decision_note": r.decision_note,
            "created_at": r.created_at, "decided_at": r.decided_at,
            "grants_on": (lambda res: {"kind": res.kind, "name": res.name} if res else None)(db.get(Resource, product.resource_id) if product else None)}


@router.post("/products/{product_id}/requests", status_code=201)
def request_access(product_id: str, body: RequestIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Ask the owners for access for one of your groups."""
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    if viewer.user.username.startswith(("service:", "sa:")):
        raise HTTPException(403, "people request access; service identities are granted by owners")
    if body.app_group_id not in viewer.groups:
        raise HTTPException(422, "choose one of your own groups")
    resource = db.get(Resource, product.resource_id)
    if _group_has(db, resource, body.app_group_id):
        raise HTTPException(409, "that group already has access")
    if db.scalar(select(AccessRequest).where(AccessRequest.product_id == product.id, AccessRequest.app_group_id == body.app_group_id,
                                             AccessRequest.status == "pending")):
        raise HTTPException(409, "a request for that group is already waiting")
    r = AccessRequest(product_id=product.id, requester=viewer.user.username, requester_principal_id=viewer.user.principal_id or "",
                      app_group_id=body.app_group_id, reason=body.reason.strip(), role=READER_ROLE.get(resource.kind, "Reader"))
    db.add(r)
    db.flush()
    audit(db, viewer.user.username, "catalog.access_request", resource.id, product=product.id, group=body.app_group_id, request=r.id)
    db.commit()
    return request_out(db, r)


def _group_has(db, resource, group_id) -> bool:
    """Whether the group can already read the object (directly or through a parent folder or source)."""
    ids = {a.id for a in rbac.ancestors(db, resource)} | {resource.id}
    for a in db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == group_id, RoleAssignment.resource_id.in_(ids))):
        target = db.get(Resource, a.resource_id)
        if "read" in rbac.roles_for(target).get(a.role, set()):
            return True
    return False


@router.get("/requests")
def list_requests(box: Literal["approve", "mine"] = "approve", status: str = "",
                  db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """approve: requests for products you own; mine: your own requests."""
    viewer = _Viewer(db, user)
    stmt = select(AccessRequest).order_by(AccessRequest.created_at.desc())
    if status:
        stmt = stmt.where(AccessRequest.status == status)
    out = []
    for r in db.scalars(stmt.limit(500)):
        product = db.get(CatalogProduct, r.product_id)
        if box == "mine":
            if r.requester == viewer.user.username:
                out.append(request_out(db, r))
        elif product and viewer.can(product.resource_id, "assign"):
            out.append(request_out(db, r))
    return out


@router.post("/requests/{request_id}/decision")
def decide(request_id: str, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Approve (grants the product's reader role to the requested group) or decline, with an optional note."""
    viewer = _Viewer(db, user)
    r = db.get(AccessRequest, request_id)
    product = db.get(CatalogProduct, r.product_id) if r else None
    if not r or not product or not viewer.can(product.resource_id, "assign"):
        raise HTTPException(404, "request not found")
    if r.status != "pending":
        raise HTTPException(409, f"this request was already {r.status}")
    resource = db.get(Resource, product.resource_id)
    if body.approve:
        if not db.get(AppGroup, r.app_group_id):
            raise HTTPException(409, "the requested group no longer exists")
        roles = list(rbac.roles_for(resource))
        current = db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == resource.id, RoleAssignment.app_group_id == r.app_group_id))
        if current is None:
            db.add(RoleAssignment(resource_id=resource.id, app_group_id=r.app_group_id, role=r.role))
        elif roles.index(current.role) < roles.index(r.role):  # never lower a role the group already has
            current.role = r.role
        audit(db, viewer.user.username, "role.assign", resource.id, group=r.app_group_id, role=r.role, request=r.id)
    r.status = "approved" if body.approve else "declined"
    r.decided_by, r.decided_at, r.decision_note = viewer.user.username, utcnow(), body.note.strip()
    audit(db, viewer.user.username, "catalog.access_" + r.status, resource.id, product=product.id, request=r.id, group=r.app_group_id)
    db.commit()
    return request_out(db, r)


@router.delete("/requests/{request_id}", status_code=204)
def withdraw(request_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    viewer = _Viewer(db, user)
    r = db.get(AccessRequest, request_id)
    if not r or r.requester != viewer.user.username:
        raise HTTPException(404, "request not found")
    if r.status == "pending":
        r.status, r.decided_at = "withdrawn", utcnow()
        audit(db, viewer.user.username, "catalog.access_withdrawn", r.product_id, request=r.id)
        db.commit()
