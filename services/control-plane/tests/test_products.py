"""Data products: owners publish objects; everyone in the layer sees the cards; access is requested and approved."""
from sqlalchemy import select

from app import catalog
from app.db import SessionLocal
from app.models import AccessRequest, AppGroup, AuditEvent, Resource, RoleAssignment
from .conftest import as_
from .rbac_helpers import grant_group
from .test_catalog import register
from .test_deploying import repo  # noqa: F401


def setup_products(client, repo):  # noqa: F811
    staging = client.post("/v1/rbac/sources/finance-dw/schemas", headers=as_("admin"), json={"name": "staging"})
    assert staging.status_code == 201, staging.text
    with SessionLocal() as db:
        public = db.scalar(select(Resource).where(Resource.kind == "schema", Resource.name == "public"))
        public_id = public.id
    for schema_id in (public_id, staging.json()["id"]):
        grant_group(client, schema_id, "Finance data", "Owner", external_groups=["finance"])
    _, dash_resource = register(client, repo, kind="app", slug="dash")
    with SessionLocal() as db:
        public, staging_schema = db.get(Resource, public_id), db.get(Resource, staging.json()["id"])
        raw = catalog.upsert(db, staging_schema, "object:raw_sales", kind="table", name="raw_sales", attributes={"object_path": "raw_sales"})
        sales = catalog.upsert(db, public, "object:sales", kind="table", name="sales", attributes={"object_path": "sales"})
        dash = catalog.upsert(db, db.get(Resource, dash_resource), kind="app")
        catalog.edge(db, staging_schema, raw, sales, "depends_on", "dbt_manifest")
        catalog.edge(db, db.get(Resource, dash_resource), sales, dash, "reads", "program_runtime")
        ids = {"raw": raw.id, "sales": sales.id, "dash": dash.id, "public": public_id}
        db.commit()
    return ids


def test_bundle_publish_browse_lineage_request_and_approve(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    # Only owners publish, and only objects people can use.
    assert client.post("/v1/catalog/products", headers=as_("bob"), json={"asset_ids": [ids["sales"]], "title": "x"}).status_code == 404  # cannot open it
    assert client.post("/v1/catalog/products", headers=as_("bob"), json={"asset_ids": [ids["dash"]], "title": "x"}).status_code == 403  # can use, not own
    schema_asset = catalog.asset_id(ids["public"])
    assert client.post("/v1/catalog/products", headers=as_("ana"), json={"asset_ids": [schema_asset], "title": "x"}).status_code in (404, 422)
    offered = client.get(f"/v1/catalog/assets/{ids['dash']}/product", headers=as_("ana")).json()
    assert offered["can_publish"] and offered["products"] == [] and [u["name"] for u in offered["uses"]] == ["sales"]
    # One product: the dashboard and the table it uses, bundled.
    published = client.post("/v1/catalog/products", headers=as_("ana"), json={
        "asset_ids": [ids["dash"], ids["sales"]], "title": "Sales", "summary": "Daily sales per region, with its table.",
        "domain": "Finance", "classification": "internal"})
    assert published.status_code == 201, published.text
    product = published.json()
    assert product["title"] == "Sales" and product["kind"] == "app" and product["domain"] == "Finance" and product["can_manage"]
    assert [(m["name"], m["kind"]) for m in product["members"]] == [("dash", "app"), ("sales", "table")]
    assert {(g["kind"], g["name"], g["role"]) for g in product["grants"]} == {("service", "dash", "Executor"), ("schema", "public", "Reader")}
    # The lead leads one product; an object may still be in others.
    assert client.post("/v1/catalog/products", headers=as_("ana"), json={"asset_ids": [ids["dash"]], "title": "again"}).status_code == 409
    in_products = client.get(f"/v1/catalog/assets/{ids['sales']}/product", headers=as_("ana")).json()["products"]
    assert [p["id"] for p in in_products] == [product["id"]]

    # Everyone in the layer sees the card; bob can use the dashboard but not the table: partly.
    cards = client.get("/v1/catalog/products", headers=as_("bob")).json()
    card = cards["items"][0]
    assert len(cards["items"]) == 1 and card["access"] == "partial" and cards["kinds"] == ["app", "table"]
    assert [m["access"] for m in card["members"]] == ["granted", "none"]
    assert card["members"][1]["open"] is None and card["members"][0]["open"]["resource_kind"] == "service"
    assert client.get("/v1/catalog/products", headers=as_("bob"), params={"accessible": True}).json()["items"] == []
    assert len(client.get("/v1/catalog/products", headers=as_("bob"), params={"kind": "table"}).json()["items"]) == 1
    assert client.get("/v1/catalog/products", headers=as_("bob"), params={"q": "sales region"}).json()["items"][0]["id"] == product["id"]
    assert client.get("/v1/catalog/products", headers=as_("bob"), params={"layer_id": "nolayer00000"}).json()["items"] == []

    # Lineage of the whole bundle: members are the product; upstream steps bob cannot open show only name, kind, owner.
    detail = client.get(f"/v1/catalog/products/{product['id']}", headers=as_("bob")).json()
    steps = {s["name"]: s for s in detail["lineage"]}
    assert set(steps) == {"raw_sales"} and steps["raw_sales"]["visible"] is False and steps["raw_sales"]["asset_id"] is None
    assert {(e["source"], e["target"]) for e in detail["lineage_edges"]} == {("member-1", "member-0"), (steps["raw_sales"]["key"], "member-1")}

    # One request covers the bundle; approval grants what each object needs.
    with SessionLocal() as db:
        executors = db.scalar(select(AppGroup).where(AppGroup.name == "Deployed executors")).id
        finance = db.scalar(select(AppGroup).where(AppGroup.name == "Finance data")).id
    url = f"/v1/catalog/products/{product['id']}/requests"
    assert client.post(url, headers=as_("bob"), json={"app_group_id": finance, "reason": "not mine"}).status_code == 422
    request = client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "Weekly sales report"})
    assert request.status_code == 201, request.text
    assert client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "again"}).status_code == 409
    assert client.get("/v1/catalog/products", headers=as_("bob")).json()["items"][0]["access"] == "requested"
    inbox = client.get("/v1/catalog/requests", headers=as_("ana")).json()
    assert [(r["product"], r["group"], r["status"]) for r in inbox] == [("Sales", "Deployed executors", "pending")]
    assert {g["name"] for g in inbox[0]["grants"]} == {"dash", "public"}
    assert client.get("/v1/catalog/requests", headers=as_("bob")).json() == []          # not an owner
    assert client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_("bob"), json={"approve": True}).status_code == 404
    decided = client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_("ana"), json={"approve": True, "note": "Welcome"})
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    assert client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_("ana"), json={"approve": False}).status_code == 409
    assert client.get("/v1/catalog/products", headers=as_("bob")).json()["items"][0]["access"] == "granted"
    with SessionLocal() as db:
        roles = {(a.resource_id, a.role) for a in db.scalars(select(RoleAssignment).where(RoleAssignment.app_group_id == executors))}
        actions = {e.action for e in db.scalars(select(AuditEvent))}
    # Access is a role on the product itself; what it gives on each object is derived, never copied.
    with SessionLocal() as db:
        from app.models import CatalogProduct
        access_resource = db.get(CatalogProduct, product["id"]).access_resource_id
    assert (access_resource, "Consumer") in roles and ("workloads", "Executor") in roles  # existing roles are kept
    assert (ids["public"], "Reader") not in roles
    assert {"catalog.publish", "catalog.access_request", "catalog.access_approved"} <= actions
    assert client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "x y z"}).status_code == 409  # has it all
    # Owners see who has access on the product page, and revoking Consumer ends it everywhere.
    detail = client.get(f"/v1/catalog/products/{product['id']}", headers=as_("ana")).json()
    assert detail["access_resource"]["kind"] == "product" and detail["access_resource"]["id"] == access_resource
    assert "access_resource" not in client.get(f"/v1/catalog/products/{product['id']}", headers=as_("bob")).json()
    sales_asset = client.get(f"/v1/catalog/resources/{ids['public']}/asset", headers=as_("bob"), params={"object_path": "sales"})
    assert sales_asset.status_code == 200  # bob may open the table now
    assert client.delete(f"/v1/rbac/resources/{access_resource}/assignments/{executors}", headers=as_("ana")).status_code == 204
    assert client.get("/v1/catalog/products", headers=as_("bob")).json()["items"][0]["access"] == "partial"
    assert client.get(f"/v1/catalog/resources/{ids['public']}/asset", headers=as_("bob"), params={"object_path": "sales"}).status_code == 404

    # Owners change what is in it; unpublishing removes the card.
    changed = client.patch(f"/v1/catalog/products/{product['id']}", headers=as_("ana"), json={"asset_ids": [ids["sales"]], "summary": "Just the table."})
    assert changed.status_code == 200 and [m["name"] for m in changed.json()["members"]] == ["sales"] and changed.json()["kind"] == "table"
    assert client.patch(f"/v1/catalog/products/{product['id']}", headers=as_("bob"), json={"title": "mine"}).status_code == 403
    assert client.delete(f"/v1/catalog/products/{product['id']}", headers=as_("bob")).status_code == 403
    assert client.delete(f"/v1/catalog/products/{product['id']}", headers=as_("ana")).status_code == 204
    assert client.get("/v1/catalog/products", headers=as_("bob")).json()["items"] == []


def test_declining_and_withdrawing(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    product = client.post("/v1/catalog/products", headers=as_("ana"), json={"asset_ids": [ids["sales"]], "title": "Sales table"}).json()
    with SessionLocal() as db:
        executors = db.scalar(select(AppGroup).where(AppGroup.name == "Deployed executors")).id
    url = f"/v1/catalog/products/{product['id']}/requests"
    first = client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "Report"}).json()
    declined = client.post(f"/v1/catalog/requests/{first['id']}/decision", headers=as_("ana"), json={"approve": False, "note": "Use the dashboard"})
    assert declined.json()["status"] == "declined" and declined.json()["decision_note"] == "Use the dashboard"
    card = next(c for c in client.get("/v1/catalog/products", headers=as_("bob")).json()["items"] if c["id"] == product["id"])
    assert card["access"] == "none"
    second = client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "Report, again"}).json()
    assert client.delete(f"/v1/catalog/requests/{second['id']}", headers=as_("ana")).status_code == 404
    assert client.delete(f"/v1/catalog/requests/{second['id']}", headers=as_("bob")).status_code == 204
    with SessionLocal() as db:
        assert db.get(AccessRequest, second["id"]).status == "withdrawn"


def test_catalog_search_leaves_files_and_folders_to_storage_and_workloads(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    with SessionLocal() as db:
        public = db.get(Resource, ids["public"])
        catalog.upsert(db, public, "object:exports/2026.csv", kind="file", name="2026.csv", attributes={"object_path": "exports/2026.csv"})
        db.commit()
    kinds = {a["kind"] for a in client.get("/v1/catalog/search", headers=as_("ana")).json()["items"]}
    assert "file" not in kinds and "folder" not in kinds and "table" in kinds
