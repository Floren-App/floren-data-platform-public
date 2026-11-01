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
