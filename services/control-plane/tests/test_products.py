"""Data products: owners publish objects; everyone in the layer sees the cards; access is requested and approved."""
from sqlalchemy import select

from app import catalog
from app.db import SessionLocal
from app.models import AccessRequest, AppGroup, AuditEvent, Resource
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


def test_publish_browse_lineage_request_and_approve(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    # Only owners publish, and only products people can use.
    assert client.post("/v1/catalog/products", headers=as_("bob"), json={"asset_id": ids["sales"]}).status_code == 404  # cannot even open it
    assert client.post("/v1/catalog/products", headers=as_("bob"), json={"asset_id": ids["dash"]}).status_code == 403  # can use, not own
    with SessionLocal() as db:
        schema_asset = catalog.asset_id(ids["public"])
    assert client.post("/v1/catalog/products", headers=as_("ana"), json={"asset_id": schema_asset}).status_code in (404, 422)
    offered = client.get(f"/v1/catalog/assets/{ids['dash']}/product", headers=as_("ana")).json()
    assert offered["can_publish"] and offered["product"] is None and [u["name"] for u in offered["uses"]] == ["sales"]
    published = client.post("/v1/catalog/products", headers=as_("ana"), json={
        "asset_id": ids["dash"], "title": "Sales dashboard", "summary": "Daily sales per region.", "domain": "Finance",
        "classification": "internal", "also": [ids["sales"]]})
    assert published.status_code == 201, published.text
    dash = published.json()
    assert dash["title"] == "Sales dashboard" and dash["domain"] == "Finance" and dash["can_manage"]

    # Everyone in the layer sees the cards, with or without access.
    cards = {c["name"]: c for c in client.get("/v1/catalog/products", headers=as_("bob")).json()["items"]}
    assert set(cards) == {"dash", "sales"}
    assert cards["dash"]["access"] == "granted"          # bob may execute deployed workloads
    assert cards["sales"]["access"] == "none" and cards["sales"]["grants"] == "Reader"
    assert cards["sales"]["grants_on"] == {"kind": "schema", "name": "public"}
    assert client.get("/v1/catalog/products", headers=as_("bob"), params={"accessible": True}).json()["items"][0]["name"] == "dash"
    assert client.get("/v1/catalog/products", headers=as_("bob"), params={"layer_id": "nolayer00000"}).json()["items"] == []

    # Lineage: steps bob cannot open show only name, kind and owner, and connect by position.
    detail = client.get(f"/v1/catalog/products/{dash['id']}", headers=as_("bob")).json()
    steps = {s["name"]: s for s in detail["lineage"]}
    assert set(steps) == {"sales", "raw_sales"}
    assert steps["sales"]["visible"] is False and steps["sales"]["asset_id"] is None and steps["sales"]["product_id"] == cards["sales"]["id"]
    assert steps["raw_sales"]["depth"] == 2 and "runtime" not in steps["raw_sales"]
    assert {(e["source"], e["target"]) for e in detail["lineage_edges"]} == {(steps["sales"]["key"], "product"), (steps["raw_sales"]["key"], steps["sales"]["key"])}
    sales_detail = client.get(f"/v1/catalog/products/{cards['sales']['id']}", headers=as_("bob")).json()
    assert [p["name"] for p in sales_detail["used_by"]] == ["dash"]

    # Request access for one of your groups; owners approve, which grants Reader on the schema.
    with SessionLocal() as db:
        executors = db.scalar(select(AppGroup).where(AppGroup.name == "Deployed executors")).id
        finance = db.scalar(select(AppGroup).where(AppGroup.name == "Finance data")).id
    url = f"/v1/catalog/products/{cards['sales']['id']}/requests"
    assert client.post(url, headers=as_("bob"), json={"app_group_id": finance, "reason": "not mine"}).status_code == 422
    request = client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "Weekly sales report"})
    assert request.status_code == 201, request.text
    assert client.post(url, headers=as_("bob"), json={"app_group_id": executors, "reason": "again"}).status_code == 409
    mine = client.get("/v1/catalog/products", headers=as_("bob")).json()["items"]
    assert next(c for c in mine if c["name"] == "sales")["access"] == "requested"
    inbox = client.get("/v1/catalog/requests", headers=as_("ana")).json()
    assert [(r["product"], r["group"], r["status"]) for r in inbox] == [("sales", "Deployed executors", "pending")]
    assert client.get("/v1/catalog/requests", headers=as_("bob")).json() == []          # not an owner
    assert client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_("bob"), json={"approve": True}).status_code == 404
    decided = client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_("ana"), json={"approve": True, "note": "Welcome"})
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    assert client.post(f"/v1/catalog/requests/{request.json()['id']}/decision", headers=as_("ana"), json={"approve": False}).status_code == 409
    cards = {c["name"]: c for c in client.get("/v1/catalog/products", headers=as_("bob")).json()["items"]}
    assert cards["sales"]["access"] == "granted"
    assert [r["status"] for r in client.get("/v1/catalog/requests", headers=as_("bob"), params={"box": "mine"}).json()] == ["approved"]
    with SessionLocal() as db:
        actions = {e.action for e in db.scalars(select(AuditEvent))}
    assert {"catalog.publish", "catalog.access_request", "catalog.access_approved"} <= actions

    # Unpublishing withdraws waiting requests and removes the card.
    second = client.post(f"/v1/catalog/products/{dash['id']}/requests", headers=as_("bob"), json={"app_group_id": executors, "reason": "x y z"})
    assert second.status_code == 409  # that group can already use the dashboard
    assert client.delete(f"/v1/catalog/products/{dash['id']}", headers=as_("bob")).status_code == 403
    assert client.delete(f"/v1/catalog/products/{dash['id']}", headers=as_("ana")).status_code == 204
    assert {c["name"] for c in client.get("/v1/catalog/products", headers=as_("bob")).json()["items"]} == {"sales"}


def test_declining_and_withdrawing(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    product = client.post("/v1/catalog/products", headers=as_("ana"), json={"asset_id": ids["sales"]}).json()
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
