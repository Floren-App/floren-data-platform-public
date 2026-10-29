"""Warehouses scoped by layer and kind of work: "this warehouse is for dbt projects in Prod".

Admission checks the scope next to the warehouse's groups for runs, deployments and Airflow workers; a declared
warehouse outside the scope falls back to the most specific default (layer and kind, the layer, the kind, then the
installation default); and scoping never strands what already relies on a warehouse."""
from app import layers
from .conftest import as_
from .test_api import start_workspace
from .test_deploying import deploy, repo  # noqa: F401
from .test_programs import put_config
from .test_warehouse_choice import adhoc, trino_on

ADMIN = as_("admin")


def scope(client, name, layer_ids=(), kinds=(), **extra):
    body = {"name": name, "cpu": 2, "memory_gb": 8, "layer_ids": list(layer_ids), "workload_kinds": list(kinds), **extra}
    return client.put(f"/v1/admin/pools/{name}", headers=ADMIN, json=body)


def default(client, name, layer_id=None, kind=None):
    body = {"name": name, **({"layer_id": layer_id} if layer_id else {}), **({"workload_kind": kind} if kind else {})}
    return client.put("/v1/admin/settings/default-warehouse", headers=ADMIN, json=body)


def test_a_warehouse_records_its_layers_and_kinds_and_rejects_unknown_ones(client):
    saved = scope(client, "prod-dbt", [layers.PROD], ["dbt"])
    assert saved.status_code == 200
    assert (saved.json()["layer_ids"], saved.json()["workload_kinds"]) == ([layers.PROD], ["dbt"])
    listed = {p["name"]: p for p in client.get("/v1/admin/pools", headers=ADMIN).json()}["prod-dbt"]
    assert listed["layer_names"] == ["Prod"]
    assert scope(client, "x", kinds=["spark"]).status_code == 422
    assert scope(client, "x", ["not-a-layer"]).status_code == 422
    assert scope(client, "anyone").json()["layer_ids"] == []   # empty: every layer and kind


def test_runs_are_admitted_only_in_the_warehouses_layers_and_kinds(client):
    ws = start_workspace(client)  # ana's workspace in Non-Prod
    scope(client, "prod-only", [layers.PROD]).raise_for_status()
    refused = adhoc(client, ws, pool="prod-only")
    assert refused.status_code == 422 and "for work in Prod, not Non-Prod" in refused.text
    scope(client, "notebooks", kinds=["notebook"]).raise_for_status()
    refused = adhoc(client, ws, pool="notebooks")
    assert refused.status_code == 422 and "for notebook work, not script" in refused.text
    scope(client, "nonprod-scripts", [layers.NON_PROD], ["script"]).raise_for_status()
    assert adhoc(client, ws, pool="nonprod-scripts").status_code == 201


def test_deployments_and_their_settings_follow_the_scope(client, repo):  # noqa: F811
    scope(client, "dbt-wh", kinds=["dbt"]).raise_for_status()
    app = deploy(client, repo, program="sales-dashboard", slug="sales", title="Sales", warehouse="dbt-wh")
    assert app.status_code == 422 and "for dbt work, not app" in app.text
    scope(client, "prod-only", [layers.PROD]).raise_for_status()
    elsewhere = deploy(client, repo, program="sales-dashboard", slug="sales", title="Sales", warehouse="prod-only",
                       layer_id=layers.NON_PROD)
    assert elsewhere.status_code == 422 and "for work in Prod" in elsewhere.text
    live = deploy(client, repo, program="sales-dashboard", slug="sales", title="Sales", warehouse="small")
    assert live.json()["status"] == "live"
    moved = client.patch(f"/v1/workloads/{live.json()['id']}", headers=as_("ana"), json={"warehouse": "dbt-wh"})
    assert moved.status_code == 422 and "not app" in moved.text


def test_defaults_per_layer_and_kind_take_the_most_specific_match(client):
    ws = start_workspace(client)
    for name in ("everything", "layer-wide", "for-scripts", "exact"):
        scope(client, name).raise_for_status()
    default(client, "everything").raise_for_status()
    assert adhoc(client, ws).json()["pool_name"] == "everything"
    assert default(client, "for-scripts", kind="script").status_code == 200
    assert adhoc(client, ws).json()["pool_name"] == "for-scripts"
    assert default(client, "layer-wide", layer_id=layers.NON_PROD).status_code == 200
    assert adhoc(client, ws).json()["pool_name"] == "layer-wide"   # the layer before the kind
    assert default(client, "exact", layer_id=layers.NON_PROD, kind="script").status_code == 200
    assert adhoc(client, ws).json()["pool_name"] == "exact"
    assert adhoc(client, ws, kind="notebook", entrypoint="x.ipynb").json()["pool_name"] == "layer-wide"
    setting = client.get("/v1/admin/settings/default-warehouse", headers=ADMIN).json()
    assert setting["name"] == "everything" and len(setting["scoped"]) == 3
    one = client.get("/v1/admin/settings/default-warehouse", headers=ADMIN,
                     params={"layer_id": layers.NON_PROD, "workload_kind": "script"}).json()
    assert one["name"] == "exact"
    cleared = client.delete("/v1/admin/settings/default-warehouse", headers=ADMIN,
                            params={"layer_id": layers.NON_PROD, "workload_kind": "script"})
    assert cleared.status_code == 204 and adhoc(client, ws).json()["pool_name"] == "layer-wide"
    client.delete("/v1/admin/settings/default-warehouse", headers=ADMIN).raise_for_status()  # only the installation default
    assert len(client.get("/v1/admin/settings/default-warehouse", headers=ADMIN).json()["scoped"]) == 2


def test_a_declared_warehouse_outside_the_scope_falls_back_to_the_scoped_default(client):
    ws = start_workspace(client)
    scope(client, "dbt-wh", kinds=["dbt"]).raise_for_status()
    default(client, "small", kind="script").raise_for_status()
    put_config(ws, config={"version": 1, "warehouse": "dbt-wh", "programs": {"hello": {"entrypoint": "hello.py"}}})
    started = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "program": "hello"})
    assert started.status_code == 201 and started.json()["pool_name"] == "small"
    explicit = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "program": "hello", "pool": "dbt-wh"})
    assert explicit.status_code == 422 and "not script" in explicit.text   # an explicit choice is not replaced


def test_scoping_never_strands_what_relies_on_a_warehouse(client, repo):  # noqa: F811
    default(client, "small").raise_for_status()
    refused = scope(client, "small", kinds=["dbt"])
    assert refused.status_code == 422 and "default warehouse for all work" in refused.text
    scope(client, "dbt-wh", kinds=["dbt"]).raise_for_status()
    assert default(client, "dbt-wh").status_code == 422          # the default for all work cannot be scoped
    assert default(client, "dbt-wh", kind="app").status_code == 422  # nor point at a warehouse not meant for it
    default(client, "dbt-wh", kind="dbt").raise_for_status()
    assert scope(client, "dbt-wh", kinds=["airflow"]).status_code == 422  # it is the dbt default
    assert client.delete("/v1/admin/pools/dbt-wh", headers=ADMIN).status_code == 409
    deploy(client, repo, program="sales-dashboard", slug="sales", title="Sales", warehouse="medium").raise_for_status()
    stranded = scope(client, "medium", kinds=["dbt"])
    assert stranded.status_code == 409 and "sales" in stranded.text
    client.put("/v1/admin/settings/workspaces", headers=ADMIN, json={"warehouse": "large", "max_running_per_user": 2}).raise_for_status()
    assert "workspaces of every layer" in scope(client, "large", [layers.PROD]).text


def test_trino_warehouses_may_only_be_limited_to_their_own_layers(client):
    scope(client, "for-dbt", kinds=["dbt"], memory_gb=16).raise_for_status()
    assert "Trino workers need a warehouse of their own" in trino_on(client, "for-dbt").text
    scope(client, "prod-wh", [layers.PROD], memory_gb=16).raise_for_status()
    assert "limited to other layers" in trino_on(client, "prod-wh").text
    assert trino_on(client, "large").status_code == 202   # Non-Prod's workers
    assert scope(client, "large", [layers.NON_PROD], memory_gb=32).status_code == 200
    assert scope(client, "large", [layers.PROD], memory_gb=32).status_code == 422
    assert scope(client, "large", [layers.NON_PROD], ["dbt"], memory_gb=32).status_code == 422


def test_run_forms_and_the_overview_see_the_scope(client):
    ws = start_workspace(client)
    scope(client, "dbt-wh", [layers.NON_PROD], ["dbt"]).raise_for_status()
    names = lambda **params: {p["name"] for p in client.get("/v1/pools", headers=as_("ana"), params=params).json()}  # noqa: E731
    assert "dbt-wh" in names(kind="dbt", layer_id=layers.NON_PROD)
    assert "dbt-wh" not in names(kind="app") and "dbt-wh" not in names(kind="dbt", layer_id=layers.PROD)
    assert "dbt-wh" in names()   # without a filter: every warehouse the person may use
    view = {w["name"]: w for w in client.get("/v1/compute/warehouses", headers=as_("ana")).json()}["dbt-wh"]
    assert (view["layer_names"], view["workload_kinds"]) == (["Non-Prod"], ["dbt"])
    assert adhoc(client, ws, pool="small").status_code == 201
    small = {w["name"]: w for w in client.get("/v1/compute/warehouses", headers=ADMIN).json()}["small"]
    assert {k["kind"]: k["in_use"] for k in small["kinds"]} == {"script": 1}


def test_warehouses_from_before_scoping_serve_every_layer_and_kind(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from app import main
    from app.models import ComputePool
    from app.schemas import PoolOut
    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE compute_pools (id VARCHAR(12) PRIMARY KEY, name VARCHAR(64), cpu FLOAT, "
                          "memory_gb FLOAT, gpu INTEGER, max_concurrent INTEGER, auto_suspend_seconds INTEGER)"))
        conn.execute(text("INSERT INTO compute_pools VALUES ('p1', 'small', 2, 8, 0, 6, 600)"))
    monkeypatch.setattr(main, "engine", old)
    main.add_missing_columns()
    from sqlalchemy.orm import Session
    with Session(old) as db:
        pool = db.get(ComputePool, "p1")
        from app.capacity import fits
        assert fits(pool, layers.PROD, "dbt")
        out = PoolOut.model_validate(pool)
        assert (out.layer_ids, out.workload_kinds) == ([], [])
