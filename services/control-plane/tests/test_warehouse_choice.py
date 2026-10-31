"""Which warehouse work runs on: an explicit choice, a declared one that exists, else the default warehouse.
A warehouse that runs Trino workers is Trino's alone. Workspace runs are private to their workspace's owner."""
from app import layers
from .conftest import as_
from .test_api import start_workspace
from .test_deploying import deploy, repo  # noqa: F401
from .test_programs import put_config

ADMIN = as_("admin")


def adhoc(client, ws, **body):
    return client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "kind": "script", "entrypoint": "x.py", **body})


def trino_on(client, warehouse):
    """Non-Prod's Trino workers on this warehouse, with the layer reported Ready."""
    from app.db import SessionLocal
    from app.models import LayerOperation
    response = client.put(f"/v1/admin/layers/{layers.NON_PROD}/infrastructure", headers=ADMIN, json={
        "expected_generation": 0, "spec": {"enabled": True, "trino": {"workers": {"warehouse": warehouse}}}})
    if response.status_code == 202:
        with SessionLocal() as db:
            db.get(LayerOperation, response.json()["operation"]["id"]).phase = "Ready"
            db.commit()
    return response


def test_work_without_a_warehouse_runs_on_the_default_one(client):
    ws = start_workspace(client)
    refused = adhoc(client, ws)
    assert refused.status_code == 422 and "no default warehouse" in refused.text
    assert client.put("/v1/admin/settings/default-warehouse", headers=ADMIN, json={"name": "medium"}).json() == {"name": "medium"}
    assert adhoc(client, ws).json()["pool_name"] == "medium"
    assert adhoc(client, ws, pool="xsmall").json()["pool_name"] == "xsmall"  # an explicit choice wins
    assert adhoc(client, ws, pool="nope").status_code == 404  # ...and must exist
    pools = {p["name"]: p for p in client.get("/v1/me", headers=as_("ana")).json()["pools"]}
    assert pools["medium"]["default"] and not pools["small"]["default"]


def test_a_program_declaring_a_missing_warehouse_falls_back_to_the_default(client):
    ws = start_workspace(client)
    client.put("/v1/admin/settings/default-warehouse", headers=ADMIN, json={"name": "xsmall"}).raise_for_status()
    put_config(ws, config={"version": 1, "warehouse": "gone", "programs": {"hello": {"entrypoint": "hello.py"}}})
    started = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "program": "hello"})
    assert started.status_code == 201 and started.json()["pool_name"] == "xsmall"


def test_the_default_warehouse_is_shared_and_kept(client):
    client.put("/v1/admin/pools/team", headers=ADMIN, json={"name": "team", "cpu": 1, "memory_gb": 2, "dedicated": True,
                                                            "allowed_groups": ["finance"]}).raise_for_status()
    assert client.put("/v1/admin/settings/default-warehouse", headers=ADMIN, json={"name": "team"}).status_code == 422
    client.put("/v1/admin/settings/default-warehouse", headers=ADMIN, json={"name": "small"}).raise_for_status()
    assert client.delete("/v1/admin/pools/small", headers=ADMIN).status_code == 409
    turned = client.put("/v1/admin/pools/small", headers=ADMIN, json={"name": "small", "cpu": 2, "memory_gb": 8, "dedicated": True,
                                                                     "allowed_groups": ["finance"]})
    assert turned.status_code == 422 and "default warehouse" in turned.text
    assert client.put("/v1/admin/settings/default-warehouse", headers=as_("ana"), json={"name": "small"}).status_code == 403


def test_only_trino_uses_a_trino_warehouse(client, repo):  # noqa: F811
    ws = start_workspace(client)
    assert trino_on(client, "large").status_code == 202
    refused = adhoc(client, ws, pool="large")
    assert refused.status_code == 422 and "only Trino uses it" in refused.text
    deployed = deploy(client, repo, program="sales-dashboard", slug="sales", title="Sales", warehouse="large")
    assert deployed.status_code == 422 and "only Trino uses it" in deployed.text
    assert client.put("/v1/admin/settings/default-warehouse", headers=ADMIN, json={"name": "large"}).status_code == 422
    assert client.put("/v1/admin/settings/workspaces", headers=ADMIN,
                      json={"warehouse": "large", "max_running_per_user": 2}).status_code == 422
    pools = {p["name"]: p for p in client.get("/v1/admin/pools", headers=ADMIN).json()}
    assert pools["large"]["trino_layers"] == ["Non-Prod"]
    # The warehouse belongs to Non-Prod: the app's layer mode shows it there only.
    assert pools["large"]["infrastructure_layer_ids"] == [layers.NON_PROD]
    assert pools["small"]["infrastructure_layer_ids"] == []


def test_runs_report_their_layer(client):
    ws = start_workspace(client)
    run = adhoc(client, ws, pool="small")
    assert run.status_code in (200, 201), run.text
    listed = {r["id"]: r for r in client.get("/v1/runs", headers=as_("ana")).json()}
    from app.db import SessionLocal
    with SessionLocal() as db:
        expected = layers.default_layer(db)
    assert listed[run.json()["id"]]["layer_id"] == expected


def test_trino_needs_a_warehouse_nothing_else_uses(client, repo):  # noqa: F811
    assert deploy(client, repo, program="sales-dashboard", slug="sales", title="Sales", warehouse="medium").json()["status"] == "live"
    taken = trino_on(client, "medium")
    assert taken.status_code == 422 and "workload sales" in taken.text
    client.put("/v1/admin/settings/default-warehouse", headers=ADMIN, json={"name": "small"}).raise_for_status()
    assert "default warehouse" in trino_on(client, "small").text


def test_workspace_runs_are_private_to_their_owner_and_administrators(client, repo):  # noqa: F811
    ws = start_workspace(client)
    mine = adhoc(client, ws, pool="xsmall").json()
    published = deploy(client, repo, program="sales-report", slug="report", title="Report").json()
    deployed = client.post(f"/v1/workloads/{published['id']}/runs", headers=as_("ana"), json={"args": {"start_date": "2026-01-01"}})
    assert deployed.status_code == 201, deployed.text
    ids = lambda user, scope="": {r["id"] for r in client.get(f"/v1/runs?scope={scope}", headers=as_(user)).json()}  # noqa: E731
    assert mine["id"] in ids("admin") and mine["id"] in ids("ana", "workspace") and mine["id"] not in ids("ana", "deployed")
    assert deployed.json()["id"] in ids("ana", "deployed")
    assert mine["id"] not in ids("bob")
    assert client.get(f"/v1/runs/{mine['id']}", headers=as_("bob")).status_code == 404
    assert client.get(f"/v1/runs/{mine['id']}", headers=ADMIN).status_code == 200
