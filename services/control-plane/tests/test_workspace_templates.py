"""Group templates give every member their own private, stopped workspace, following memberships."""
from app import layers
from .conftest import as_
from .test_capacity import tick

ADMIN = as_("admin")


def group(client, name, *users):
    me = client.get("/v1/me", headers=ADMIN).json()
    g = client.post("/v1/rbac/groups", headers=ADMIN, json={"name": name, "owners": [me["principal_id"]]}).json()
    for user in users:
        join(client, g, user)
    return g


def join(client, g, user):
    principal = client.get("/v1/me", headers=as_(user)).json()["principal_id"]
    r = client.post(f"/v1/rbac/groups/{g['id']}/bindings", headers=ADMIN, json={"principal_id": principal})
    assert r.status_code == 201, r.text
    return r.json()


def template(client, g, **changes):
    body = {"name": "Data engineering", "group_id": g["id"], "editor": "vscode", "layer_id": layers.NON_PROD, **changes}
    r = client.post("/v1/admin/workspace-templates", headers=ADMIN, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def mine(client, user):
    return [w for w in client.get("/v1/workspaces", headers=as_(user)).json() if w.get("template_id")]


def test_every_member_gets_their_own_stopped_workspace(client):
    g = group(client, "Data engineers", "ana")
    t = template(client, g, editor="rstudio")
    [ws] = mine(client, "ana")
    assert (ws["name"], ws["editor"], ws["status"], ws["layer_id"], ws["template_id"]) == ("Data engineering", "rstudio", "stopped", layers.NON_PROD, t["id"])
    assert mine(client, "bob") == []
    join(client, g, "bob")  # a later member gets theirs too
    tick()
    [bobs] = mine(client, "bob")
    assert bobs["id"] != ws["id"]  # never shared
    listed = client.get("/v1/admin/workspace-templates", headers=ADMIN).json()
    assert listed[0]["workspaces"] == 2 and listed[0]["group_name"] == "Data engineers"


def test_leaving_removes_it_and_a_deleted_one_stays_deleted(client):
    g = group(client, "Analysts", "ana", "bob")
    template(client, g)
    [anas] = mine(client, "ana")
    assert client.delete(f"/v1/workspaces/{anas['id']}", headers=as_("ana")).status_code == 204
    tick()
    assert mine(client, "ana") == []  # deleted on purpose: not recreated
    bob = client.get("/v1/me", headers=as_("bob")).json()["principal_id"]
    assert mine(client, "bob")
    assert client.delete(f"/v1/rbac/groups/{g['id']}/bindings/{bob}", headers=ADMIN).status_code == 204
    tick()
    assert mine(client, "bob") == []  # left the group


def test_template_changes_follow_and_deleting_the_template_removes_its_workspaces(client):
    g = group(client, "Modellers", "ana")
    t = template(client, g)
    r = client.put(f"/v1/admin/workspace-templates/{t['id']}", headers=ADMIN, json={
        "name": "Modelling", "group_id": g["id"], "editor": "sql", "layer_id": layers.NON_PROD})
    assert r.status_code == 200, r.text
    [ws] = mine(client, "ana")
    assert (ws["name"], ws["editor"]) == ("Modelling", "sql")
    assert client.delete(f"/v1/admin/workspace-templates/{t['id']}", headers=ADMIN).status_code == 204
    assert mine(client, "ana") == []


def test_only_admins_and_only_layers_that_allow_workspaces(client):
    g = group(client, "Team", "ana")
    body = {"name": "x", "group_id": g["id"], "editor": "vscode", "layer_id": layers.NON_PROD}
    assert client.post("/v1/admin/workspace-templates", headers=as_("ana"), json=body).status_code == 403
    client.put(f"/v1/admin/layers/{layers.PROD}", headers=ADMIN, json={"name": "Prod", "workspaces": False, "production": True})
    refused = client.post("/v1/admin/workspace-templates", headers=ADMIN, json={**body, "layer_id": layers.PROD})
    assert refused.status_code == 403 and "disabled in this layer" in refused.text
    assert client.post("/v1/admin/workspace-templates", headers=ADMIN, json={**body, "group_id": "nope"}).status_code == 422
