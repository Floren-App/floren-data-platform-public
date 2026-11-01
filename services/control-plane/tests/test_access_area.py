"""The Access area: what a group, person or service identity can reach, and through what."""
from .conftest import as_
from .rbac_helpers import grant_group
from .test_deploying import repo  # noqa: F401
from .test_products import setup_products


def test_group_and_person_access_show_roles_products_and_where_they_come_from(client, repo):  # noqa: F811
    ids = setup_products(client, repo)
    # bob reaches Readers through his IdP group, and Viewers directly.
    readers = grant_group(client, ids["public"], "Readers", "Reader", external_groups=["marketing"])
    viewers = grant_group(client, "workloads", "Viewers", "Executor", users=["bob"])
    product = client.post("/v1/catalog/products", headers=as_("ana"), json={"asset_ids": [ids["dash"], ids["sales"]], "title": "Sales"})
    assert product.status_code == 201, product.text
    client.put(f"/v1/catalog/products/{product.json()['id']}/consumers/{viewers}", headers=as_("ana"), json={}).raise_for_status()

    group = client.get(f"/v1/rbac/groups/{viewers}/access", headers=as_("admin")).json()
    rows = {(r["resource"]["name"], r["role"], r["via"], (r["product"] or {}).get("title")) for r in group["grants"]}
    assert ("Sales", "Consumer", "direct", None) in rows                 # the product itself
    assert ("public", "Reader", "product", "Sales") in rows              # its table's schema, through the product
    assert any(r[2] == "product" and r[1] == "Executor" for r in rows)   # its dashboard
    assert group["compliance_roles"] == [] and group["app_profile"] is None
    assert client.get(f"/v1/rbac/groups/{viewers}/access", headers=as_("ana")).status_code == 403

    me = client.get("/v1/me", headers=as_("bob")).json()
    person = client.get(f"/v1/rbac/principals/{me['principal_id']}/access", headers=as_("bob")).json()
    assert {(g["name"], g["via"]) for g in person["groups"]} >= {("Readers", "marketing"), ("Viewers", None)}
    assert {(r["resource"]["name"], r["group"]["name"]) for r in person["grants"] if r["via"] == "direct"} >= {("public", "Readers"), ("Sales", "Viewers")}
    # Administrators and the person themselves; nobody else.
    assert client.get(f"/v1/rbac/principals/{me['principal_id']}/access", headers=as_("admin")).status_code == 200
    assert client.get(f"/v1/rbac/principals/{me['principal_id']}/access", headers=as_("ana")).status_code == 403
    assert readers


def test_the_audit_log_filters_by_who_what_and_object(client):
    from app.db import SessionLocal
    from app.models import AuditEvent
    with SessionLocal() as db:
        db.add_all([AuditEvent(actor="ana", action="rbac.assign", target="res1", detail={}),
                    AuditEvent(actor="bob", action="rbac.assign", target="res2", detail={}),
                    AuditEvent(actor="ana", action="workspace.start", target="ws1", detail={})])
        db.commit()
    get = lambda **q: [(e["actor"], e["action"], e["target"]) for e in client.get("/v1/admin/audit", headers=as_("admin"), params=q).json()]  # noqa: E731
    assert get(actor="an", action="rbac") == [("ana", "rbac.assign", "res1")]
    assert get(target="res2") == [("bob", "rbac.assign", "res2")]
    assert client.get("/v1/admin/audit", headers=as_("ana")).status_code == 403


def test_each_access_and_administration_page_is_granted_on_its_own(client):
    from .rbac_helpers import grant_group
    forms = grant_group(client, "set-forms", "Form editors", "Manager", users=["bob"])
    form = {"name": "Purpose", "definition": {"pages": [{"id": "p", "title": "P", "fields": [{"id": "why", "label": "Why", "type": "short_text"}]}]}}
    assert client.put("/v1/compliance/forms/Purpose", headers=as_("bob"), json=form).status_code == 200
    # Manager on compliance forms is not Manager on classifications.
    levels = client.get("/v1/catalog/classifications", headers=as_("bob")).json()["levels"]
    refused = client.put("/v1/catalog/classifications", headers=as_("bob"), json={"levels": levels})
    assert refused.status_code == 403 and "Manager on Classifications" in refused.text
    # Viewer on the audit log reads it and changes nothing.
    grant_group(client, "set-auditlog", "Auditors", "Viewer", users=["ana"])
    assert client.get("/v1/admin/audit", headers=as_("ana")).status_code == 200
    assert client.put("/v1/admin/settings/egress", headers=as_("ana"), json={"hosts": []}).status_code == 403
    # A section covers its pages.
    grant_group(client, "adm-privacy", "Form editors", "Manager", users=["bob"])
    assert client.put("/v1/catalog/classifications", headers=as_("bob"), json={"levels": levels}).status_code == 200
    me = client.get("/v1/me", headers=as_("bob")).json()
    assert me["settings"]["set-classes"] == "manage" and me["settings"]["set-forms"] == "manage" and "set-layers" not in me["settings"]
    assert client.get("/v1/me", headers=as_("admin")).json()["settings"]["set-layers"] == "manage"
    assert forms


def test_alert_rules_send_runs_slas_and_requests_to_the_right_channel(client, repo, monkeypatch):  # noqa: F811
    import json
    from datetime import timedelta
    from app import notify
    from app.db import SessionLocal
    from app.models import CatalogAsset, ComputePool, Run, SlaIncident, Workload, utcnow
    posted = []
    monkeypatch.setattr(notify, "_post", lambda url, body, headers: posted.append((url, json.loads(body))) or 200)
    ids = setup_products(client, repo)
    for name in ("ops", "data"):
        client.put(f"/v1/admin/webhooks/{name}", headers=as_("admin"), json={"name": name, "url": f"https://hooks.test/{name}"}).raise_for_status()
    hooks = {h["name"]: h["id"] for h in client.get("/v1/admin/webhooks", headers=as_("admin")).json()["webhooks"]}
    with SessionLocal() as db:
        workload = db.query(Workload).first()
        folder = db.get(__import__("app.models", fromlist=["Resource"]).Resource, workload.resource_id).parent_id
    # Failed runs under that folder to ops; SLA failures of the public schema to data.
    rule = client.post("/v1/alerts/rules", headers=as_("admin"), json={"name": "Failed runs", "events": ["run.failed"], "channel_id": hooks["ops"],
                                                                       "resource_ids": [folder]})
    assert rule.status_code == 201, rule.text
    client.post("/v1/alerts/rules", headers=as_("admin"), json={"name": "SLA", "events": ["sla.breached", "sla.recovered"], "channel_id": hooks["data"],
                                                                "resource_ids": [ids["public"]]}).raise_for_status()
    # Someone who owns no object cannot make a rule; an object's owner can, for that object.
    assert client.post("/v1/alerts/rules", headers=as_("bob"), json={"name": "x", "events": ["run.failed"], "channel_id": hooks["ops"],
                                                                     "resource_ids": [folder]}).status_code == 403
    mine = client.post("/v1/alerts/rules", headers=as_("ana"), json={"name": "Requests", "events": ["access.requested"], "channel_id": hooks["data"],
                                                                     "resource_ids": [ids["public"]]})
    assert mine.status_code == 201, mine.text
    with SessionLocal() as db:
        pool = db.query(ComputePool).first()
        later = utcnow() + timedelta(seconds=1)
        db.add(Run(owner="ana", workload_id=workload.id, pool_id=pool.id, kind="script", entrypoint="x.py", status="failed", finished_at=later))
        asset = db.get(CatalogAsset, ids["sales"])
        db.add(SlaIncident(asset_id=asset.id, status="breached", opened_at=later, updated_at=later))
        db.commit()
    notify.tick(SessionLocal)
    notify.tick(SessionLocal)   # once
    sent = [(url.rsplit("/", 1)[1], body["event"]) for url, body in posted]
    assert sorted(sent) == [("data", "sla.breached"), ("ops", "run.failed")]
    assert all(body["subject"] for _, body in posted)
    listed = client.get("/v1/alerts/rules", headers=as_("ana"), params={"resource_id": ids["public"]}).json()
    assert [r["name"] for r in listed] == ["Requests", "SLA"]   # the schema's owner sees every rule on it


def test_platform_administrators_see_the_pages_in_listings_to_grant_them(client):
    listed = {r["id"]: r for r in client.get("/v1/rbac/resources", headers=as_("admin")).json()}
    assert {"admin-root", "adm-privacy", "set-forms"} <= set(listed) and "assign" in listed["set-forms"]["actions"]
    assert "set-forms" not in {r["id"] for r in client.get("/v1/rbac/resources", headers=as_("ana")).json()}


def test_an_administrator_sees_the_pages_of_the_group_they_act_as(client):
    from .rbac_helpers import grant_group
    office = grant_group(client, "workloads", "Privacy office", "Executor", users=["admin"])
    client.post("/v1/compliance/roles", headers=as_("admin"), json={"role": "compliance_admin", "app_group_id": office}).raise_for_status()
    full = client.get("/v1/me", headers=as_("admin")).json()
    assert full["acting_admin"] and len(full["settings"]) == 16
    assert client.put("/v1/me/active-group", headers=as_("admin"), json={"group_id": office}).status_code == 200
    acting = client.get("/v1/me", headers=as_("admin")).json()
    assert not acting["acting_admin"] and acting["compliance_roles"] == ["compliance_admin"]
    assert set(acting["settings"]) == {"set-classes", "set-policies", "set-forms", "set-duties"}
    # The API still lets administrators do everything.
    assert client.get("/v1/admin/audit", headers=as_("admin")).status_code == 200
    assert client.put("/v1/me/active-group", headers=as_("admin"), json={"group_id": "000000000001"}).status_code == 200
    assert len(client.get("/v1/me", headers=as_("admin")).json()["settings"]) == 16
