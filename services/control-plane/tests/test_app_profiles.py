"""App profiles: which parts of the app a group gets, enforced by the API and following the active group."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AppGroup
from .conftest import as_
from .rbac_helpers import grant_group


def me(client, user):
    return client.get("/v1/me", headers=as_(user)).json()


def make_viewer(client):
    """bob in a group "Viewers" on the Viewer profile, as his active group."""
    client.put("/v1/admin/app-profiles/Viewer", headers=as_("admin"), json={"name": "Viewer", "features": ["products.browse"]}).raise_for_status()
    gid = grant_group(client, "workloads", "Viewers", "Executor", users=["bob"])
    group = client.get(f"/v1/rbac/groups", headers=as_("admin")).json()
    body = next(g for g in group if g["id"] == gid)
    updated = client.put(f"/v1/rbac/groups/{gid}", headers=as_("admin"), json={
        "name": body["name"], "description": body["description"], "owners": body["owners"], "app_profile": "Viewer"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["app_profile"] == "Viewer"
    assert client.put("/v1/me/active-group", headers=as_("bob"), json={"group_id": gid}).status_code == 200
    return gid


def test_no_profiles_until_configured_and_profiles_follow_the_active_group(client):
    listed = client.get("/v1/app-profiles", headers=as_("ana")).json()
    assert listed["profiles"] == []   # the platform comes with none: Terraform or an administrator adds them
    assert {f["key"] for f in listed["features"]} >= {"workspaces", "products.browse", "security"}
    # Without profiles, nothing is limited: object permissions decide, as before.
    assert "workspaces" in me(client, "ana")["app_features"] and me(client, "ana")["app_profile"] is None
    gid = make_viewer(client)
    bob = me(client, "bob")
    assert bob["app_profile"] == "Viewer" and bob["app_features"] == ["products.browse"]
    # Enforced by the API, not only hidden.
    refused = client.post("/v1/workspaces", headers=as_("bob"), json={"name": "x", "editor": "vscode"})
    assert refused.status_code == 403 and "app profile" in refused.text
    assert client.get("/v1/workspaces", headers=as_("bob")).status_code == 403
    assert client.get("/v1/catalog/search", headers=as_("bob")).status_code == 403
    assert client.get("/v1/catalog/products", headers=as_("bob")).status_code == 200
    assert client.post("/v1/catalog/products", headers=as_("bob"), json={"asset_ids": ["x"], "title": "x"}).status_code == 403
    assert client.get("/v1/rbac/resources", headers=as_("bob")).status_code == 200   # object permissions only
    assert client.get("/v1/me", headers=as_("bob")).status_code == 200
    # Saving the group without the field keeps its profile.
    body = next(g for g in client.get("/v1/rbac/groups", headers=as_("admin")).json() if g["id"] == gid)
    kept = client.put(f"/v1/rbac/groups/{gid}", headers=as_("admin"), json={"name": body["name"], "description": "x", "owners": body["owners"]})
    assert kept.json()["app_profile"] == "Viewer"
    # Administrators have everything with Global Admin active; with another group active they see its profile.
    assert set(me(client, "admin")["app_features"]) >= {"workspaces", "security", "workloads.manage"}
    client.post(f"/v1/rbac/groups/{gid}/bindings", headers=as_("admin"), json={"principal_id": me(client, "admin")["principal_id"]}).raise_for_status()
    assert client.put("/v1/me/active-group", headers=as_("admin"), json={"group_id": gid}).status_code == 200
    acting = me(client, "admin")
    assert acting["app_profile"] == "Viewer" and acting["app_features"] == ["products.browse"]
    assert client.get("/v1/workspaces", headers=as_("admin")).status_code == 200   # the API still lets administrators do everything
    assert client.put("/v1/me/active-group", headers=as_("admin"), json={"group_id": "000000000001"}).status_code == 200
    assert me(client, "admin")["app_profile"] == "Full access (administrator)"


def test_only_administrators_configure_profiles_and_features_bring_what_they_need(client):
    body = {"name": "Requesters", "description": "Find and request", "features": ["products.request"]}
    assert client.put("/v1/admin/app-profiles/Requesters", headers=as_("ana"), json=body).status_code == 403
    created = client.put("/v1/admin/app-profiles/Requesters", headers=as_("admin"), json=body)
    assert created.status_code == 200 and created.json()["features"] == ["products.browse", "products.request"]
    assert client.put("/v1/admin/app-profiles/Bad", headers=as_("admin"), json={"name": "Bad", "features": ["teleport"]}).status_code == 422
    # At most one default: groups without a profile follow it.
    client.put("/v1/admin/app-profiles/Analyst", headers=as_("admin"), json={"name": "Analyst", "features": ["storage"], "default": True}).raise_for_status()
    switched = client.put("/v1/admin/app-profiles/Requesters", headers=as_("admin"), json={**body, "default": True})
    assert switched.json()["default"]
    defaults = [p["name"] for p in client.get("/v1/app-profiles", headers=as_("ana")).json()["profiles"] if p["default"]]
    assert defaults == ["Requesters"]
    assert me(client, "ana")["app_features"] == ["products.browse", "products.request"]
    # Without a default again, groups without a profile get everything.
    client.put("/v1/admin/app-profiles/Requesters", headers=as_("admin"), json={**body, "default": False}).raise_for_status()
    assert "workspaces" in me(client, "ana")["app_features"]
    # A profile in use cannot be removed; others can.
    make_viewer(client)
    assert client.delete("/v1/admin/app-profiles/Viewer", headers=as_("admin")).status_code == 409
    assert client.delete("/v1/admin/app-profiles/Requesters", headers=as_("admin")).status_code == 204
    assert client.delete("/v1/admin/app-profiles/Analyst", headers=as_("admin")).status_code == 204
    with SessionLocal() as db:
        assert db.scalar(select(AppGroup).where(AppGroup.name == "Viewers")).profile_id
