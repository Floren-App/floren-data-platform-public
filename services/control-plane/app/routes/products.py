"""Data products: one or more catalog objects their owners published together, for others to find,
understand and request. A dashboard bundled with the tables it uses is a typical product.

A product's card (name, summary, owner, domain, what is in it, freshness) is visible to everyone in its layer,
with or without access. Its lineage from the sources shows the steps by name, kind and owner only; details
(columns, preview, SQL) stay behind each object's own permissions. One access request covers the whole
product. Access is a role on the product itself: groups with Consumer on it may read or use everything in it
(rbac.product_grants), so revoking it, removing an object or unpublishing ends that access everywhere."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import catalog as c, layers, rbac
from ..auth import User
from ..db import get_db
from ..deps import get_actor
from ..models import (AccessRequest, AppGroup, CatalogAsset, CatalogProduct, CatalogProductMember, LineageEdge, ProductOption,
                      Resource, RoleAssignment, Workload, utcnow)
from .common import audit

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])

PUBLISHABLE = {"table", "view", "materialized_view", "app", "api", "topic"}
TABLE_KINDS = {"table", "view", "materialized_view"}
READER_ROLE = rbac.READER_ROLE
DATA_RELATIONS = ("reads", "writes", "depends_on", "uses")
PATH_DEPTH, PATH_LIMIT, MAX_MEMBERS = 6, 40, 20


class MemberRef(BaseModel):
    """An object by its RBAC resource, and for a table its name in the schema (Terraform refers to objects this way)."""
    model_config = ConfigDict(extra="forbid")
    resource_id: str = Field(min_length=1, max_length=12)
    object_path: Optional[str] = Field(default=None, max_length=1024, description="A table or view in a schema resource")


class MaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: Optional[str] = None
    resource_id: Optional[str] = None
    table: Optional[str] = Field(default=None, max_length=1024)
    column: str = Field(min_length=1, max_length=256)
    method: Literal["null", "redact", "hash", "partial", "expression"] = "hash"
    expression: str = Field(default="", max_length=4000)


class FilterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: Optional[str] = None
    resource_id: Optional[str] = None
    table: Optional[str] = Field(default=None, max_length=1024)
    expression: str = Field(min_length=1, max_length=4000)


class OptionIn(BaseModel):
    """A way to use the product: masks and row filters on its tables for the groups that get this option."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    default: bool = False
    masks: List[MaskIn] = Field(default_factory=list, max_length=200)
    filters: List[FilterIn] = Field(default_factory=list, max_length=50)


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_ids: List[str] = Field(default_factory=list, max_length=MAX_MEMBERS, description="Objects in the product; the first leads (its icon)")
    members: List[MemberRef] = Field(default_factory=list, max_length=MAX_MEMBERS, description="Objects by resource (after asset_ids)")
    options: List[OptionIn] = Field(default_factory=list, max_length=10, description="Access options; none: one Standard option without masks")
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=600)
    domain: str = Field(default="", max_length=80)
    classification: Optional[Literal["public", "internal", "confidential", "restricted"]] = None


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    summary: Optional[str] = Field(default=None, max_length=600)
    asset_ids: Optional[List[str]] = Field(default=None, max_length=MAX_MEMBERS,
                                           description="Replace the objects in the product; the first leads")
    members: Optional[List[MemberRef]] = Field(default=None, max_length=MAX_MEMBERS, description="Replace the objects, by resource")
    options: Optional[List[OptionIn]] = Field(default=None, max_length=10, description="Replace the access options")


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_group_id: str = Field(min_length=1, max_length=12)
    reason: str = Field(min_length=3, max_length=1000)
    option_id: Optional[str] = Field(default=None, max_length=12, description="The access option asked for; default: the default option")


class ConsumerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: Optional[str] = Field(default=None, max_length=12, description="Default: the default option")


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


def _aware(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=utcnow().tzinfo)


class _Viewer:
    """Per-request permission snapshot of the viewer."""

    def __init__(self, db: Session, user: User):
        self.db, self.user = db, rbac.resolve_user(db, user)
        _, self.actions = c.permissions(db, self.user)
        self.groups = rbac.app_groups(db, self.user)

    def can(self, resource_id: str, action: str) -> bool:
        return action in self.actions.get(resource_id, set())


def members(db: Session, product: CatalogProduct) -> List[tuple]:
    """(asset, resource) per live object in the product, lead first."""
    out = []
    for m in db.scalars(select(CatalogProductMember).where(CatalogProductMember.product_id == product.id)
                        .order_by(CatalogProductMember.position, CatalogProductMember.id)):
        asset = db.get(CatalogAsset, m.asset_id)
        resource = db.get(Resource, asset.resource_id) if asset else None
        if asset and _live(db, resource):
            out.append((asset, resource))
    return out


def _grants(parts) -> List[dict]:
    """What Consumer on the product gives: one role per distinct resource (tables share their schema's)."""
    seen, out = set(), []
    for asset, resource in parts:
        if resource.id not in seen:
            seen.add(resource.id)
            out.append({"resource_id": resource.id, "kind": resource.kind, "name": resource.name,
                        "role": READER_ROLE.get(resource.kind, "Reader"), "object": asset.name})
    return out


def _open(asset, resource):
    return {"resource_kind": resource.kind, "resource_id": resource.id, "object_id": resource.object_id,
            "object_path": (asset.attributes or {}).get("object_path")}


def card(db: Session, product: CatalogProduct, viewer: _Viewer) -> dict:
    parts = members(db, product)
    lead_asset, lead_resource = parts[0] if parts else (db.get(CatalogAsset, product.asset_id), db.get(Resource, product.resource_id))
    governance = lead_asset.governance or {}
    pending = db.scalar(select(AccessRequest).where(AccessRequest.product_id == product.id, AccessRequest.status == "pending",
                                                    AccessRequest.requester == viewer.user.username))
    readable = [viewer.can(r.id, "read") for _, r in parts]
    access = "granted" if parts and all(readable) else "requested" if pending else "partial" if any(readable) else "none"
    fresh = [_aware(_freshness(db, a, r)) for a, r in parts]
    fresh = [f for f in fresh if f]
    level = c.service_level(db, lead_asset, lead_resource)
    member_out = []
    for asset, resource in parts:
        can = viewer.can(resource.id, "read")
        member_out.append({"asset_id": asset.id, "name": asset.name, "kind": asset.kind, "runtime": _runtime(db, resource),
                           "resource_id": resource.id, "object_path": (asset.attributes or {}).get("object_path") if asset.kind in TABLE_KINDS else None,
                           "access": "granted" if can else "none", "updated_at": _freshness(db, asset, resource),
                           "open": _open(asset, resource) if can else None})
    from .. import classification as levels, product_options
    manager = bool(product.access_resource_id) and viewer.can(product.access_resource_id, "assign")
    option_out = []
    for option in product_options.options(db, product.id):
        item = product_options.out(db, parts, option)
        if not manager:  # column names and filters are details: for owners
            item = {k: item[k] for k in ("id", "name", "description", "default", "classification")} | {
                "masked_columns": len(option.masks or []), "filtered_tables": len(option.filters or [])}
        option_out.append(item)
    return {"id": product.id, "asset_id": lead_asset.id, "title": product.title, "summary": product.summary,
            "kind": lead_asset.kind, "runtime": _runtime(db, lead_resource), "name": lead_asset.name,
            **layers.info(db, lead_resource),
            "domain": governance.get("domain") or "",
            # The level of what is in it, unmasked; each option says what its consumers see.
            "classification": levels.highest(db, (levels.effective(db, a) for a, _ in parts)),
            "options": option_out,
            "status": governance.get("status", "draft"), "steward": governance.get("steward") or "",
            "owner_group_name": _owner(db, lead_asset, lead_resource),
            "updated_at": max(fresh) if fresh else None, "sla_status": level.get("status"),
            "published_at": product.published_at, "published_by": product.published_by,
            "access": access, "members": member_out,
            "can_manage": bool(product.access_resource_id) and viewer.can(product.access_resource_id, "assign"),
            "grants": _grants(parts),
            # Where to open the lead object itself: only for people who may.
            "open": _open(lead_asset, lead_resource) if viewer.can(lead_resource.id, "read") else None}


def _visible_products(db: Session, viewer: _Viewer, layer_id: str = ""):
    stmt = select(CatalogProduct)
    if layer_id:
        stmt = stmt.where(CatalogProduct.layer_id == layer_id)
    out = []
    for product in db.scalars(stmt.order_by(CatalogProduct.title)):
        resource = db.get(Resource, product.resource_id)
        if _live(db, resource) and _in_layer(db, viewer.user, resource) and members(db, product):
            out.append(product)
    return out


@router.get("/products")
def list_products(layer_id: str = "", q: str = Query(default="", max_length=200), domain: str = "", kind: str = "",
                  accessible: bool = False, mine: bool = False,
                  db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Published data products in a layer, for everyone in it. kind: products containing that kind of object;
    accessible: only ones you can fully use; mine: only ones you manage."""
    viewer = _Viewer(db, user)
    cards = [card(db, p, viewer) for p in _visible_products(db, viewer, layer_id)]
    terms = q.casefold().split()

    def text(x):
        return " ".join([str(x[k] or "") for k in ("title", "summary", "domain", "owner_group_name")]
                        + [m["name"] + " " + m["kind"] for m in x["members"]]).casefold()
    kinds = sorted({m["kind"] for x in cards for m in x["members"]})
    cards = [x for x in cards if (not domain or x["domain"] == domain) and (not kind or any(m["kind"] == kind for m in x["members"]))
             and (not accessible or x["access"] == "granted") and (not mine or x["can_manage"])
             and all(t in text(x) for t in terms)]
    return {"items": cards, "domains": sorted({x["domain"] for x in cards if x["domain"]}), "kinds": kinds}


@router.get("/products/candidates")
def publish_candidates(layer_id: str = "", q: str = Query(default="", max_length=200),
                       db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Objects you may put in a product: tables and views, dashboards, APIs and topics you own."""
    viewer = _Viewer(db, user)
    c.sync_registry(db)
    out = []
    for asset in db.scalars(select(CatalogAsset).where(CatalogAsset.kind.in_(PUBLISHABLE))):
        resource = db.get(Resource, asset.resource_id)
        if not _live(db, resource) or not viewer.can(resource.id, "assign"):
            continue
        where = layers.info(db, resource)
        if layer_id and where["layer_id"] not in (layer_id, None):
            continue
        if q and q.casefold() not in (asset.name + " " + asset.path).casefold():
            continue
        out.append({"asset_id": asset.id, "name": asset.name, "kind": asset.kind, "path": asset.path, **where})
    db.commit()
    return sorted(out, key=lambda a: (a["kind"] != "app", a["kind"], a["name"].casefold()))[:300]


def _upstream(db: Session, starts, depth: int = 1) -> Dict[str, int]:
    seen = {ident: 0 for ident in starts}
    frontier = set(starts)
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
    """The products an object is in, and what it uses directly that you could bundle with it."""
    asset, resource = c.require_asset(db, ident, user)
    viewer = _Viewer(db, user)
    product_ids = set(db.scalars(select(CatalogProductMember.product_id).where(CatalogProductMember.asset_id == asset.id)))
    products = [card(db, p, viewer) for p in (db.get(CatalogProduct, i) for i in product_ids) if p]
    uses = []
    if asset.kind in PUBLISHABLE:
        for other_id in _upstream(db, [asset.id], 1):
            other = db.get(CatalogAsset, other_id)
            if not other or other.id == asset.id or other.kind not in PUBLISHABLE - {"app"}:
                continue
            res = db.get(Resource, other.resource_id)
            if _live(db, res) and viewer.can(res.id, "assign") and layers.info(db, res)["layer_id"] == layers.info(db, resource)["layer_id"]:
                uses.append({"asset_id": other.id, "name": other.name, "kind": other.kind})
    return {"publishable": asset.kind in PUBLISHABLE, "can_publish": viewer.can(resource.id, "assign"),
            "products": products, "uses": uses, "name": asset.name, "kind": asset.kind,
            "domain": (asset.governance or {}).get("domain") or "",
            "classification": (asset.governance or {}).get("classification", "unclassified")}


def _checked_members(db, viewer, asset_ids) -> List[tuple]:
    """The objects for a product: publishable, live, owned by the viewer, in one layer, no duplicates."""
    parts, layer = [], None
    for ident in dict.fromkeys(asset_ids):
        asset = db.get(CatalogAsset, ident)
        resource = db.get(Resource, asset.resource_id) if asset else None
        if not asset or not _live(db, resource) or not viewer.can(resource.id, "read"):
            raise HTTPException(404, "catalog object not available")
        if asset.kind not in PUBLISHABLE:
            raise HTTPException(422, f"{asset.name}: products hold tables and views, dashboards, APIs or Kafka topics")
        if not viewer.can(resource.id, "assign"):
            raise HTTPException(403, f"only the owners of {asset.name} can publish it")
        where = layers.info(db, resource)["layer_id"]
        if parts and where != layer:
            raise HTTPException(422, "everything in a product must be in the same layer")
        layer = where
        parts.append((asset, resource))
    return parts


def _resolve(db, viewer, asset_ids, members) -> List[str]:
    """Asset ids of the objects named by asset id or by resource (and table); unknown tables are indexed."""
    ids = list(asset_ids or [])
    if members:
        c.sync_registry(db)
    for ref in members or []:
        resource = db.get(Resource, ref.resource_id)
        if resource is None or not viewer.can(resource.id, "read"):
            raise HTTPException(404, "catalog object not available")
        if ref.object_path:
            if resource.kind != "schema":
                raise HTTPException(422, "object_path names a table or view in a schema")
            path = ref.object_path.strip()
            ident = c.asset_id(resource.id, "object:" + path)
            if db.get(CatalogAsset, ident) is None and viewer.can(resource.id, "assign"):
                c.upsert(db, resource, "object:" + path, kind="table", name=path,
                         attributes={"object_path": path, "schema": resource.name, "origin": "data_product"})
                db.flush()
            ids.append(ident)
        else:
            ids.append(c.asset_id(resource.id))
    if not ids:
        raise HTTPException(422, "a product needs at least one object")
    return ids


def _options_for(db, product, parts, given) -> None:
    """Save given options, or re-check the current ones against changed contents (masks on removed tables go)."""
    from .. import product_options
    if given is not None:
        product_options.save(db, product, product_options.validate(parts, [o.model_dump() for o in given]))
        return
    current = product_options.options(db, product.id)
    tables = {a.id for a, _ in parts}
    kept = [{"name": o.name, "description": o.description, "default": o.default,
             "masks": [m for m in o.masks or [] if m["asset_id"] in tables],
             "filters": [f for f in o.filters or [] if f["asset_id"] in tables]} for o in current]
    product_options.save(db, product, product_options.validate(parts, kept))


def _set_members(db, product, parts):
    db.execute(delete(CatalogProductMember).where(CatalogProductMember.product_id == product.id))
    db.flush()
    for i, (asset, _) in enumerate(parts):
        db.add(CatalogProductMember(product_id=product.id, asset_id=asset.id, position=i))
    product.asset_id, product.resource_id = parts[0][0].id, parts[0][1].id
    product.layer_id = layers.info(db, parts[0][1])["layer_id"]
    access = db.get(Resource, product.access_resource_id) if product.access_resource_id else None
    if access is not None:
        access.config = {**(access.config or {}), "layer_id": product.layer_id}


@router.post("/products", status_code=201)
def publish(body: PublishIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Publish one or more objects you own as a data product."""
    viewer = _Viewer(db, user)
    parts = _checked_members(db, viewer, _resolve(db, viewer, body.asset_ids, body.members))
    lead = parts[0][0]
    if db.scalar(select(CatalogProduct).where(CatalogProduct.asset_id == lead.id)):
        raise HTTPException(409, f"{lead.name} already leads a product: add to that one, or lead with another object")
    governance = dict(lead.governance or {})
    if body.domain and not governance.get("domain"):
        governance["domain"] = body.domain.strip()
    if body.classification and governance.get("classification", "unclassified") == "unclassified":
        governance["classification"] = body.classification
    if governance != (lead.governance or {}):
        lead.governance = governance
        c.reindex(lead)
    product = CatalogProduct(asset_id=lead.id, resource_id=parts[0][1].id, title=body.title.strip(), summary=body.summary.strip(),
                             published_by=viewer.user.username)
    db.add(product)
    db.flush()
    # The product's own permissions: the publisher's active group owns it; consumers are added by approval.
    access = Resource(kind="product", name=product.title, parent_id=rbac.products_root(db).id, object_id=product.id, config={})
    db.add(access)
    db.flush()
    product.access_resource_id = access.id
    rbac.own(db, viewer.user, access)
    _set_members(db, product, parts)
    _options_for(db, product, parts, body.options)
    audit(db, viewer.user.username, "catalog.publish", product.resource_id, product=product.id, title=product.title,
          objects=[a.id for a, _ in parts])
    db.commit()
    return card(db, product, _Viewer(db, user))  # a fresh snapshot: it includes the new product's permissions


def _product(db, product_id, viewer) -> CatalogProduct:
    product = db.get(CatalogProduct, product_id)
    resource = db.get(Resource, product.resource_id) if product else None
    if not product or not _live(db, resource) or not _in_layer(db, viewer.user, resource):
        raise HTTPException(404, "data product not found")
    return product


def _require_manager(db, product, viewer, what):
    if not _manages(db, viewer, product):
        raise HTTPException(403, f"only the product's owners can {what}")


@router.patch("/products/{product_id}")
def update_product(product_id: str, body: ProductPatch, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    _require_manager(db, product, viewer, "change it")
    if body.title is not None:
        product.title = body.title.strip()
        access = db.get(Resource, product.access_resource_id) if product.access_resource_id else None
        if access is not None:
            access.name = product.title
    if body.summary is not None:
        product.summary = body.summary.strip()
    if body.asset_ids is not None or body.members is not None:
        parts = _checked_members(db, viewer, _resolve(db, viewer, body.asset_ids, body.members))
        other = db.scalar(select(CatalogProduct).where(CatalogProduct.asset_id == parts[0][0].id, CatalogProduct.id != product.id))
        if other:
            raise HTTPException(409, f"{parts[0][0].name} already leads another product")
        _set_members(db, product, parts)
        _options_for(db, product, parts, body.options)
    elif body.options is not None:
        _options_for(db, product, members(db, product), body.options)
    product.updated_at = utcnow()
    audit(db, viewer.user.username, "catalog.product_update", product.resource_id, product=product.id)
    db.commit()
    return card(db, product, viewer)


@router.delete("/products/{product_id}", status_code=204)
def unpublish(product_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    _require_manager(db, product, viewer, "unpublish it")
    for request in db.scalars(select(AccessRequest).where(AccessRequest.product_id == product.id, AccessRequest.status == "pending")):
        request.status, request.decided_by, request.decided_at = "withdrawn", viewer.user.username, utcnow()
        request.decision_note = "The product was unpublished."
    audit(db, viewer.user.username, "catalog.unpublish", product.resource_id, product=product.id)
    db.execute(delete(CatalogProductMember).where(CatalogProductMember.product_id == product.id))
    from ..models import ProductAccess, ProductOption
    db.execute(delete(ProductAccess).where(ProductAccess.product_id == product.id))
    db.execute(delete(ProductOption).where(ProductOption.product_id == product.id))
    if product.access_resource_id:  # access given through the product ends with it
        from ..models import ServiceRoleAssignment
        db.execute(delete(RoleAssignment).where(RoleAssignment.resource_id == product.access_resource_id))
        db.execute(delete(ServiceRoleAssignment).where(ServiceRoleAssignment.resource_id == product.access_resource_id))
        access = db.get(Resource, product.access_resource_id)
        if access is not None:
            db.delete(access)
    db.delete(product)
    db.commit()


@router.get("/products/{product_id}")
def product_detail(product_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """The card, the lineage of everything in it (from the sources), and the products built on it.
    Steps you may not open show only their name, kind and owner."""
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    parts = members(db, product)
    own = [a.id for a, _ in parts]
    layer = product.layer_id
    in_products: Dict[str, str] = {}
    for m in db.scalars(select(CatalogProductMember).where(CatalogProductMember.product_id != product.id)):
        in_products.setdefault(m.asset_id, m.product_id)
    # Upstream of every object in the product; stay in its layer; skip operational objects; files show as their bucket.
    seen = _upstream(db, own, PATH_DEPTH)
    path, real, kept = [], [], set(own)
    for ident, depth in sorted(seen.items(), key=lambda item: item[1]):
        if ident in own or len(path) >= PATH_LIMIT:
            continue
        node = db.get(CatalogAsset, ident)
        resource = db.get(Resource, node.resource_id) if node else None
        if not node or not _live(db, resource) or layers.info(db, resource)["layer_id"] not in (layer, None):
            continue
        if (node.governance or {}).get("lineage_role") == "operational" or (node.attributes or {}).get("inactive"):
            continue
        if node.kind == "file":
            bucket = db.get(CatalogAsset, c.asset_id(resource.id))
            if bucket is None:
                continue
            node = bucket
        if node.id in kept:
            continue
        kept.add(node.id)
        visible = viewer.can(resource.id, "read")
        step = {"key": f"step-{len(path)}", "asset_id": node.id if visible else None, "name": node.name, "kind": node.kind,
                "depth": depth, "owner_group_name": _owner(db, node, resource), "visible": visible,
                "product_id": in_products.get(node.id)}
        if visible:
            step["runtime"] = _runtime(db, resource)
        path.append(step)
        real.append(node.id)
    # Steps are keyed by position and members as member-i, so hidden steps connect without exposing their ids.
    key_of = {ident: f"step-{i}" for i, ident in enumerate(real)}
    key_of.update({ident: f"member-{i}" for i, ident in enumerate(own)})
    edges = sorted({(key_of[e.source_id], key_of[e.target_id]) for e in db.scalars(select(LineageEdge).where(
        LineageEdge.target_id.in_(kept), LineageEdge.source_id.in_(kept), LineageEdge.relation.in_(DATA_RELATIONS)))
        if e.source_id in key_of and e.target_id in key_of and e.source_id != e.target_id})
    # Downstream: other products built on anything in this one (named for everyone), and how many other objects use it.
    downstream, frontier = set(), set(own)
    for _ in range(4):
        found = set()
        for e in db.scalars(select(LineageEdge).where(LineageEdge.source_id.in_(frontier), LineageEdge.relation.in_(DATA_RELATIONS))):
            if e.target_id not in downstream and e.target_id not in own:
                downstream.add(e.target_id)
                found.add(e.target_id)
        frontier = found
        if not frontier:
            break
    used_by, seen_products, others = [], set(), 0
    for ident in downstream:
        other_id = in_products.get(ident)
        if other_id:
            if other_id not in seen_products:
                seen_products.add(other_id)
                other = db.get(CatalogProduct, other_id)
                if other and other.layer_id == layer and _live(db, db.get(Resource, other.resource_id)):
                    used_by.append(card(db, other, viewer))
        else:
            node = db.get(CatalogAsset, ident)
            resource = db.get(Resource, node.resource_id) if node else None
            if node and _live(db, resource) and node.kind != "file":
                others += 1
    out = card(db, product, viewer)
    out.update({"lineage": path, "used_by": used_by, "used_by_others": others,
                "lineage_edges": [{"source": a, "target": b} for a, b in edges]})
    access = db.get(Resource, product.access_resource_id) if product.access_resource_id else None
    if access is not None and out["can_manage"]:  # owners manage who has access on the product page
        from .rbac import resource_out
        from .. import product_options
        out["access_resource"] = resource_out(db, access, viewer.user)
        consumers = []
        for a in db.scalars(select(RoleAssignment).where(RoleAssignment.resource_id == access.id, RoleAssignment.role == "Consumer")):
            group, option = db.get(AppGroup, a.app_group_id), product_options.option_for_group(db, product.id, a.app_group_id)
            consumers.append({"group_id": a.app_group_id, "group": group.name if group else a.app_group_id,
                              "option_id": option.id if option else None, "option": option.name if option else None})
        out["consumers"] = sorted(consumers, key=lambda x: x["group"].casefold())
    db.commit()
    return out


# --- access requests ---------------------------------------------------------------------------------------

def request_out(db, r: AccessRequest) -> dict:
    product = db.get(CatalogProduct, r.product_id)
    group = db.get(AppGroup, r.app_group_id)
    return {"id": r.id, "product_id": r.product_id, "product": product.title if product else "", "requester": r.requester,
            "app_group_id": r.app_group_id, "group": group.name if group else r.app_group_id, "reason": r.reason,
            "status": r.status, "decided_by": r.decided_by, "decision_note": r.decision_note,
            "created_at": r.created_at, "decided_at": r.decided_at,
            "option_id": r.option_id, "option": (lambda o: o.name if o else None)(db.get(ProductOption, r.option_id) if r.option_id else None),
            "grants": _grants(members(db, product)) if product else []}


def _group_has(db, resource, group_id) -> bool:
    """Whether the group can already read the object (directly or through a parent folder or source)."""
    if resource is None:
        return False
    ids = {a.id for a in rbac.ancestors(db, resource)} | {resource.id}
    for a in db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == group_id, RoleAssignment.resource_id.in_(ids))):
        target = db.get(Resource, a.resource_id)
        if "read" in rbac.roles_for(target).get(a.role, set()):
            return True
    return False


@router.post("/products/{product_id}/requests", status_code=201)
def request_access(product_id: str, body: RequestIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Ask the owners for access to the whole product, for one of your groups."""
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    if viewer.user.username.startswith(("service:", "sa:")):
        raise HTTPException(403, "people request access; service identities are granted by owners")
    if body.app_group_id not in viewer.groups:
        raise HTTPException(422, "choose one of your own groups")
    parts = members(db, product)
    if _group_has(db, db.get(Resource, product.access_resource_id), body.app_group_id) or (
            parts and all(_group_has(db, r, body.app_group_id) for _, r in parts)):
        raise HTTPException(409, "that group already has access to everything in this product")
    if db.scalar(select(AccessRequest).where(AccessRequest.product_id == product.id, AccessRequest.app_group_id == body.app_group_id,
                                             AccessRequest.status == "pending")):
        raise HTTPException(409, "a request for that group is already waiting")
    from .. import product_options
    option = db.get(ProductOption, body.option_id) if body.option_id else product_options.default_option(db, product.id)
    if option is None or option.product_id != product.id:
        raise HTTPException(422, "choose one of the product's access options")
    r = AccessRequest(product_id=product.id, requester=viewer.user.username, requester_principal_id=viewer.user.principal_id or "",
                      app_group_id=body.app_group_id, reason=body.reason.strip(), role="Consumer", option_id=option.id)
    db.add(r)
    db.flush()
    audit(db, viewer.user.username, "catalog.access_request", product.resource_id, product=product.id, group=body.app_group_id, request=r.id)
    db.commit()
    return request_out(db, r)


def _manages(db, viewer, product) -> bool:
    return bool(product and product.access_resource_id) and viewer.can(product.access_resource_id, "assign")


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
        if box == "mine":
            if r.requester == viewer.user.username:
                out.append(request_out(db, r))
        elif _manages(db, viewer, db.get(CatalogProduct, r.product_id)):
            out.append(request_out(db, r))
    return out


@router.post("/requests/{request_id}/decision")
def decide(request_id: str, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Approve (grants each object's reader role to the requested group) or decline, with an optional note."""
    viewer = _Viewer(db, user)
    r = db.get(AccessRequest, request_id)
    product = db.get(CatalogProduct, r.product_id) if r else None
    if not r or not _manages(db, viewer, product):
        raise HTTPException(404, "request not found")
    if r.status != "pending":
        raise HTTPException(409, f"this request was already {r.status}")
    if body.approve:
        if not db.get(AppGroup, r.app_group_id):
            raise HTTPException(409, "the requested group no longer exists")
        # Consumer on the product itself; what that gives on each object is derived (rbac.product_grants).
        current = db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == product.access_resource_id,
                                                         RoleAssignment.app_group_id == r.app_group_id))
        if current is None:
            db.add(RoleAssignment(resource_id=product.access_resource_id, app_group_id=r.app_group_id, role="Consumer"))
            audit(db, viewer.user.username, "role.assign", product.access_resource_id, group=r.app_group_id, role="Consumer", request=r.id)
        from .. import product_options
        option = db.get(ProductOption, r.option_id) if r.option_id else None
        option = option if option is not None and option.product_id == product.id else product_options.default_option(db, product.id)
        if option is not None:
            product_options.set_option(db, product.id, r.app_group_id, option.id)
    r.status = "approved" if body.approve else "declined"
    r.decided_by, r.decided_at, r.decision_note = viewer.user.username, utcnow(), body.note.strip()
    audit(db, viewer.user.username, "catalog.access_" + r.status, product.resource_id, product=product.id, request=r.id, group=r.app_group_id)
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


@router.put("/products/{product_id}/consumers/{group_id}")
def put_consumer(product_id: str, group_id: str, body: ConsumerIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Make a group a consumer of the product with an option, or change its option (product owners)."""
    from .. import product_options
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    _require_manager(db, product, viewer, "give access")
    if not db.get(AppGroup, group_id):
        raise HTTPException(422, "choose an existing app group")
    option = db.get(ProductOption, body.option_id) if body.option_id else product_options.default_option(db, product.id)
    if option is None or option.product_id != product.id:
        raise HTTPException(422, "choose one of the product's access options")
    current = db.scalar(select(RoleAssignment).where(RoleAssignment.resource_id == product.access_resource_id,
                                                     RoleAssignment.app_group_id == group_id))
    if current is None:
        db.add(RoleAssignment(resource_id=product.access_resource_id, app_group_id=group_id, role="Consumer"))
    product_options.set_option(db, product.id, group_id, option.id)
    audit(db, viewer.user.username, "catalog.consumer", product.access_resource_id, product=product.id, group=group_id, option=option.name)
    db.commit()
    return {"group_id": group_id, "option_id": option.id, "option": option.name}


@router.delete("/products/{product_id}/consumers/{group_id}", status_code=204)
def delete_consumer(product_id: str, group_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Take a group's access through the product away (product owners)."""
    from ..models import ProductAccess
    viewer = _Viewer(db, user)
    product = _product(db, product_id, viewer)
    _require_manager(db, product, viewer, "take access away")
    db.execute(delete(RoleAssignment).where(RoleAssignment.resource_id == product.access_resource_id,
                                            RoleAssignment.app_group_id == group_id, RoleAssignment.role == "Consumer"))
    db.execute(delete(ProductAccess).where(ProductAccess.product_id == product.id, ProductAccess.app_group_id == group_id))
    audit(db, viewer.user.username, "catalog.consumer_removed", product.access_resource_id, product=product.id, group=group_id)
    db.commit()
