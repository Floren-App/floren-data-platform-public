"""Access options of data products: the masks and row filters a product's consumers get on its tables.

A consuming group has one option (ProductAccess; the default option when none is recorded). The option's
masks and filters apply to people who reach a table only through data products: someone who can read the
table's schema directly is not limited by a product. Reaching a table through several options gives the
least restrictive view: a column is masked only if every option masks it, and rows are filtered only if every
option filters them (any of the options' filters then admits a row). The table's own masks and filters
(DataPolicy) always apply as well."""
from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import select

from . import classification, data_policies, rbac
from .models import CatalogAsset, CatalogProduct, CatalogProductMember, ProductAccess, ProductOption, Resource

TABLE_KINDS = {"table", "view", "materialized_view"}
DEFAULT_OPTION = {"name": "Standard", "description": "What the product's tables show to everyone.", "default": True,
                  "masks": [], "filters": []}


def _member(parts, ref: dict, what: str):
    """The product's table a mask or filter refers to: by asset_id, or by resource_id and table."""
    for asset, resource in parts:
        if asset.kind not in TABLE_KINDS:
            continue
        if ref.get("asset_id") and ref["asset_id"] == asset.id:
            return asset
        table = (ref.get("table") or "").lower()
        if table and (asset.attributes or {}).get("object_path", asset.name).lower() == table and \
                (not ref.get("resource_id") or ref["resource_id"] == resource.id):
            return asset
    raise HTTPException(422, f"{what} must refer to a table in the product")


def validate(parts, options: List[dict]) -> List[dict]:
    """Options with their masks and filters checked and tied to the product's tables. One is the default."""
    if not options:
        return [dict(DEFAULT_OPTION)]
    names = [o["name"].strip() for o in options]
    if len(set(n.lower() for n in names)) != len(names) or not all(names):
        raise HTTPException(422, "option names must be unique and not empty")
    out = []
    for option in options:
        masks, filters, seen = [], [], set()
        for mask in option.get("masks") or []:
            asset = _member(parts, mask, "a mask")
            column = data_policies.normalize_name(mask.get("column"), "the masked column")
            if (asset.id, column) in seen:
                raise HTTPException(422, f"{option['name']}: {asset.name}.{column} is masked twice")
            seen.add((asset.id, column))
            method, expression = data_policies.validate("column_mask", mask.get("method") or "hash", mask.get("expression") or "", column)
            masks.append({"asset_id": asset.id, "column": column, "method": method, "expression": expression})
        filtered = set()
        for item in option.get("filters") or []:
            asset = _member(parts, item, "a row filter")
            if asset.id in filtered:
                raise HTTPException(422, f"{option['name']}: one row filter per table")
            filtered.add(asset.id)
            _, expression = data_policies.validate("row_filter", "", item.get("expression") or "", "")
            filters.append({"asset_id": asset.id, "expression": expression})
        out.append({"name": option["name"].strip(), "description": (option.get("description") or "").strip(),
                    "default": bool(option.get("default")), "masks": masks, "filters": filters})
    defaults = [o for o in out if o["default"]]
    if len(defaults) > 1:
        raise HTTPException(422, "one option is the default")
    if not defaults:
        out[0]["default"] = True
    return out


def options(db, product_id: str) -> List[ProductOption]:
    return list(db.scalars(select(ProductOption).where(ProductOption.product_id == product_id)
                           .order_by(ProductOption.position, ProductOption.name)))


def default_option(db, product_id: str) -> Optional[ProductOption]:
    found = options(db, product_id)
    return next((o for o in found if o.default), found[0] if found else None)


def save(db, product: CatalogProduct, validated: List[dict]) -> None:
    """Replace the options; an option keeps its id when its name stays, so consumers keep it.
    Consumers of a removed option move to the default option."""
    existing = {o.name.lower(): o for o in options(db, product.id)}
    kept = set()
    for i, item in enumerate(validated):
        option = existing.get(item["name"].lower())
        if option is None:
            option = ProductOption(product_id=product.id)
            db.add(option)
        option.name, option.description, option.default = item["name"], item["description"], item["default"]
        option.masks, option.filters, option.position = item["masks"], item["filters"], i
        kept.add(item["name"].lower())
    db.flush()
    default = default_option(db, product.id)
    for name, option in existing.items():
        if name not in kept:
            for access in db.scalars(select(ProductAccess).where(ProductAccess.option_id == option.id)):
                access.option_id = default.id
            db.delete(option)
    db.flush()


def option_for_group(db, product_id: str, group_id: str) -> Optional[ProductOption]:
    access = db.scalar(select(ProductAccess).where(ProductAccess.product_id == product_id, ProductAccess.app_group_id == group_id))
    option = db.get(ProductOption, access.option_id) if access else None
    return option or default_option(db, product_id)


def set_option(db, product_id: str, group_id: str, option_id: str) -> None:
    access = db.scalar(select(ProductAccess).where(ProductAccess.product_id == product_id, ProductAccess.app_group_id == group_id))
    if access is None:
        db.add(ProductAccess(product_id=product_id, app_group_id=group_id, option_id=option_id))
    else:
        access.option_id = option_id


def level(db, parts, option: Optional[ProductOption]) -> str:
    """The classification consumers of this option see: per table its own level, masked columns at their masked level."""
    masked: Dict[str, set] = {}
    for mask in (option.masks if option else []) or []:
        masked.setdefault(mask["asset_id"], set()).add(mask["column"])
    return classification.highest(db, (classification.effective(db, asset, masked.get(asset.id, ())) for asset, _ in parts))


def out(db, parts, option: ProductOption) -> dict:
    names = {asset.id: (asset.attributes or {}).get("object_path", asset.name) for asset, _ in parts}
    owners = {asset.id: resource.id for asset, resource in parts}
    where = lambda item: {**item, "table": names.get(item["asset_id"], ""), "resource_id": owners.get(item["asset_id"], "")}
    return {"id": option.id, "name": option.name, "description": option.description, "default": option.default,
            "classification": level(db, parts, option),
            "masks": [where(m) for m in option.masks or []], "filters": [where(f) for f in option.filters or []]}


def attach(db, user, scope: dict) -> dict:
    """Add the masks and filters of the caller's product options to an admitted SQL scope (see module docstring)."""
    if user.is_admin and not user.execution_context:
        return scope
    groups = rbac.app_groups(db, user)
    accesses = list(db.scalars(select(ProductAccess).where(ProductAccess.app_group_id.in_(groups)))) if groups else []
    grants = rbac.role_grants(db, user)
    product_resources = {g.product_resource_id for g in grants if isinstance(g, rbac.ProductGrant)}
    if not product_resources:
        return scope
    # Schemas the caller reads directly (not through a product) are not limited by product options.
    direct = set()
    for g in grants:
        if isinstance(g, rbac.ProductGrant):
            continue
        target = db.get(Resource, g.resource_id)
        if target is not None and "read" in rbac.roles_for(target).get(g.role, set()):
            direct.add(g.resource_id)
    # Every path (product option) through which the caller reaches each table.
    paths: Dict[str, List[ProductOption]] = {}
    for product in db.scalars(select(CatalogProduct).where(CatalogProduct.access_resource_id.in_(product_resources))):
        consumer_groups = {g.app_group_id for g in grants if isinstance(g, rbac.ProductGrant)
                           and g.product_resource_id == product.access_resource_id}
        chosen = []
        for group in consumer_groups:
            access = next((a for a in accesses if a.product_id == product.id and a.app_group_id == group), None)
            option = db.get(ProductOption, access.option_id) if access else None
            chosen.append(option or default_option(db, product.id))
        for member in db.scalars(select(CatalogProductMember).where(CatalogProductMember.product_id == product.id)):
            paths.setdefault(member.asset_id, []).extend(chosen)
    group_names = sorted(data_policies.caller_groups(db, user)[1])
    for catalog, item in scope.items():
        if item.get("system_metadata"):
            continue
        for schema_name in item.get("schemas", {}):
            schema = db.scalar(select(Resource).where(Resource.kind == "schema", Resource.object_id == item["source_id"],
                                                      Resource.name == schema_name))
            if schema is None or {a.id for a in rbac.ancestors(db, schema)} & direct or schema.id in direct:
                continue
            for asset in db.scalars(select(CatalogAsset).where(CatalogAsset.resource_id == schema.id,
                                                               CatalogAsset.kind.in_(TABLE_KINDS))):
                options_here = paths.get(asset.id)
                if not options_here:
                    continue
                table = (asset.attributes or {}).get("object_path", asset.name).lower()
                masked = None  # columns every path masks
                for option in options_here:
                    columns = {m["column"]: m for m in (option.masks if option else []) or [] if m["asset_id"] == asset.id}
                    masked = columns if masked is None else {c: m for c, m in masked.items() if c in columns}
                filters = []
                for option in options_here:
                    found = next((f for f in (option.filters if option else []) or [] if f["asset_id"] == asset.id), None)
                    if found is None:
                        filters = []
                        break
                    filters.append(found["expression"])
                if not masked and not filters:
                    continue
                entry = item.setdefault("policies", {}).setdefault(schema_name + "." + table, {"filter": None, "masks": {}})
                for column, mask in (masked or {}).items():
                    entry["masks"].setdefault(column, {"method": mask["method"], "expression": mask["expression"]})
                if filters:
                    combined = " OR ".join("(" + data_policies.render(f, group_names) + ")" for f in filters)
                    entry["filter"] = f"({entry['filter']}) AND ({combined})" if entry.get("filter") else combined
                item["groups"] = group_names
    return scope
