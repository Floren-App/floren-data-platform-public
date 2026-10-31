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
