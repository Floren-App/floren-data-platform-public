"""Warehouse limits count every container on a warehouse; work beyond them queues and starts in order."""
import json

from app import capacity, deps
from app.config import get_settings
from app.db import SessionLocal
from app.models import ComputePool, Run, Workload, Workspace
from app.reaper import reap_once
from .conftest import as_
from .test_api import start_workspace
from .test_deploying import deploy, repo  # noqa: F401


def test_queued_dbt_plan_preserves_schema_and_reads_existing_queue_records():
    from app.executions import RunPlan
    from app.programs import DbtSettings
    from app.routes.runs import plan_from_record, plan_record
    plan = RunPlan(kind='dbt', entrypoint='dbt/project',runtime='dbt',
                   dbt=DbtSettings(data_source='lake',schema='pokemon_history'))
    record = plan_record(plan)
    assert record['dbt']['schema'] == 'pokemon_history'
    assert plan_from_record(record).dbt == plan.dbt
    record['dbt']['schema_'] = record['dbt'].pop('schema')
    assert plan_from_record(record).dbt == plan.dbt


def limit(name, n):
    with SessionLocal() as db:
        pool = db.query(ComputePool).filter_by(name=name).one()
        pool.max_concurrent = n
        db.commit()


def run(client, ws_id, user="ana"):
    response = client.post("/v1/runs", headers=as_(user), json={
        "workspace_id": ws_id, "kind": "script", "entrypoint": "x.py", "pool": "small"})
    assert response.status_code == 201, response.text
    return response.json()


def finish(run_id):
    deps.get_orchestrator().states["run-" + run_id] = "succeeded"


def tick():
    with SessionLocal() as db:
        reap_once(db, deps.get_orchestrator(), get_settings())


def status(run_id):
    with SessionLocal() as db:
        return db.get(Run, run_id).status


def test_jobs_beyond_the_limit_queue_and_start_in_arrival_order(client):
    limit("small", 1)
    ws = start_workspace(client)
    first, second, third = run(client, ws), run(client, ws), run(client, ws)
    assert first["status"] == "running"
    assert (second["status"], second["queue_position"]) == ("queued", 1)
    assert (third["status"], third["queue_position"]) == ("queued", 2)
    assert "run-" + second["id"] not in deps.get_orchestrator().executions  # nothing started yet
    small = next(p for p in client.get("/v1/admin/pools", headers=as_("admin")).json() if p["name"] == "small")
    assert (small["in_use"], small["queued"], small["max_concurrent"]) == (1, 2, 1)

    tick()
    assert status(second["id"]) == "queued"  # still full
    finish(first["id"])
    tick()
    assert (status(first["id"]), status(second["id"]), status(third["id"])) == ("succeeded", "running", "queued")
    assert client.get(f"/v1/runs/{third['id']}", headers=as_("ana")).json()["queue_position"] == 1


def test_new_work_cannot_overtake_the_queue(client):
    limit("small", 1)
    ws = start_workspace(client)
    first, waiting = run(client, ws), run(client, ws)
    finish(first["id"])
    with SessionLocal() as db:  # the slot is free, but the dispatcher has not run yet
        from app.reaper import refresh_run
        refresh_run(db, deps.get_orchestrator(), db.get(Run, first["id"]))
        db.commit()
    late = run(client, ws)
    assert late["status"] == "queued" and late["queue_position"] == 2
    tick()
    assert (status(waiting["id"]), status(late["id"])) == ("running", "queued")


def test_a_queued_job_keeps_secret_paths_never_values_and_reads_them_at_start(client):
    limit("small", 1)
    ws = start_workspace(client)
    assert client.put("/v1/secrets/user/ana/api-token", headers=as_("ana"), json={"value": "s3cret-value"}).status_code == 200
    first = run(client, ws)
    response = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws, "kind": "script", "entrypoint": "x.py", "pool": "small"})
    queued = response.json()
    with SessionLocal() as db:
        row = db.get(Run, queued["id"])
        row.launch["plan"]["secret_refs"] = {"API_TOKEN": "user/api-token"}
        row.launch["secrets"] = {"API_TOKEN": "env/user/ana/api-token"}
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(row, "launch")
        db.commit()
        assert "s3cret-value" not in json.dumps(db.get(Run, queued["id"]).launch)
    finish(first["id"])
    tick()
    spec = deps.get_orchestrator().executions["run-" + queued["id"]]
    assert spec.secret_env["API_TOKEN"] == "s3cret-value"
    with SessionLocal() as db:
        assert db.get(Run, queued["id"]).launch is None


def test_stopping_a_queued_job_cancels_it(client):
    limit("small", 1)
    ws = start_workspace(client)
    run(client, ws)
    queued, behind = run(client, ws), run(client, ws)
    stopped = client.post(f"/v1/runs/{queued['id']}/stop", headers=as_("ana")).json()
    assert stopped["status"] == "stopped" and stopped["queue_position"] is None
    assert client.get(f"/v1/runs/{behind['id']}", headers=as_("ana")).json()["queue_position"] == 1


def test_a_deploy_waits_for_capacity_but_an_update_does_not(client, repo):  # noqa: F811
    limit("xsmall", 1)
    ws = start_workspace(client)
    job = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws, "kind": "script", "entrypoint": "x.py", "pool": "xsmall"}).json()
    assert job["status"] == "running"
    pub = deploy(client, repo, program="sales-dashboard", slug="sales", title="Sales").json()
    assert pub["status"] == "queued" and pub["queue_position"] == 1 and "capacity on warehouse xsmall" in pub["sync_error"]
    assert not any(name.startswith("pub-" + pub["id"]) for name in deps.get_orchestrator().executions)
    finish(job["id"])
    tick()
    live = client.get(f"/v1/workloads/{pub['id']}", headers=as_("ana")).json()
    assert live["status"] == "live" and live["queue_position"] is None and live["queued_sha"] is None
    # Full again, but an update replaces the running app: it needs no extra slot.
    second = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws, "kind": "script", "entrypoint": "x.py", "pool": "xsmall"}).json()
    assert second["status"] == "queued"  # the app holds xsmall's only slot
    with SessionLocal() as db:
        from app.deploying import deploy as deploy_now, get_git
        workload = db.get(Workload, pub["id"])
        assert deploy_now(db, deps.get_orchestrator(), get_git(), workload, workload.deployed_sha, "restart")
        assert db.get(Workload, pub["id"]).status == "live"


def test_distributed_airflow_counts_every_component_on_its_warehouse():
    pub = Workload(id="w1", slug="w", title="w", repo_id="r", branch="main", program="p", kind="airflow",
                   runtime="python", owner="ana", warehouse="small", status="live",
                   program_def={"airflow_settings": {"mode": "distributed", "workers": 2, "worker_warehouse": "large"}},
                   execution_refs=["pub-w1-broker", "pub-w1-metadata", "pub-w1-abc-web", "pub-w1-abc-scheduler",
                                   "pub-w1-abc-worker-1", "pub-w1-abc-worker-2"])
    assert capacity.workload_held(pub) == {"small": 2, "large": 2}  # broker and metadata are not warehouse compute

    class Settings:
        mode, workers, worker_warehouse = "distributed", 3, None

    class Program:
        airflow_settings = Settings()
    assert capacity.demand(pub, Program()) == {"small": 5}


def test_workspaces_have_their_own_total_and_per_person_limits(client):
    response = client.put("/v1/admin/settings/workspaces", headers=as_("admin"),
                          json={"warehouse": "medium", "max_running": 1, "max_running_per_user": 2})
    assert response.status_code == 200, response.text
    assert client.put("/v1/admin/settings/workspaces", headers=as_("ana"),
                      json={"max_running_per_user": 9}).status_code == 403
    assert client.put("/v1/admin/settings/workspaces", headers=as_("admin"),
                      json={"warehouse": "nope", "max_running_per_user": 2}).status_code == 422
    limit("medium", 1)  # workspaces never count toward a warehouse's limit
    first = start_workspace(client)
    spec = deps.get_orchestrator().executions["ws-" + first]
    assert (spec.cpu, spec.memory_gb) == (4, 16)  # sized by the administrator's workspace warehouse
    second = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "second", "editor": "vscode"}).json()
    queued = client.post(f"/v1/workspaces/{second['id']}/start", headers=as_("ana")).json()
    assert (queued["status"], queued["queue_position"]) == ("queued", 1)
    third = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "third", "editor": "vscode"}).json()
    refused = client.post(f"/v1/workspaces/{third['id']}/start", headers=as_("ana"))
    assert refused.status_code == 409  # a queued workspace counts toward the person's own limit
    client.post(f"/v1/workspaces/{first}/stop", headers=as_("ana"))
    tick()
    with SessionLocal() as db:
        assert db.get(Workspace, second["id"]).status == "running"
    settings = client.get("/v1/admin/settings/workspaces", headers=as_("admin")).json()
    assert (settings["running"], settings["queued"], settings["warehouse"]) == (1, 0, "medium")


def test_workspace_settings_reset_and_protect_their_warehouse(client):
    client.put("/v1/admin/settings/workspaces", headers=as_("admin"),
               json={"warehouse": "xlarge", "max_running": 3, "max_running_per_user": 1})
    assert client.delete("/v1/admin/pools/xlarge", headers=as_("admin")).status_code == 409
    assert client.delete("/v1/admin/settings/workspaces", headers=as_("admin")).status_code == 204
    settings = client.get("/v1/admin/settings/workspaces", headers=as_("admin")).json()
    assert (settings["warehouse"], settings["max_running"]) == (None, None)
    assert settings["max_running_per_user"] == get_settings().max_running_workspaces_per_user
    assert client.delete("/v1/admin/pools/xlarge", headers=as_("admin")).status_code == 204


def test_an_existing_installation_keeps_its_warehouse_limits_after_the_rename(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from app import main
    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE compute_pools (id VARCHAR(12) PRIMARY KEY, name VARCHAR(64), cpu FLOAT, "
                          "memory_gb FLOAT, gpu INTEGER, max_concurrent_runs INTEGER NOT NULL, auto_suspend_seconds INTEGER)"))
        conn.execute(text("INSERT INTO compute_pools VALUES ('p1', 'small', 2, 8, 0, 6, 600)"))
    monkeypatch.setattr(main, "engine", old)
    main.add_missing_columns()
    main.add_missing_columns()  # idempotent
    with old.connect() as conn:
        assert conn.execute(text("SELECT max_concurrent FROM compute_pools")).scalar() == 6


def test_the_warehouse_overview_shows_use_per_layer_and_only_details_you_may_see(client, repo):  # noqa: F811
    limit("xsmall", 2)
    ws = start_workspace(client)
    mine = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws, "kind": "script", "entrypoint": "mine.py", "pool": "xsmall"}).json()
    app = deploy(client, repo, program="sales-dashboard", slug="sales", title="Sales").json()
    assert app["status"] == "live"
    waiting = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws, "kind": "script", "entrypoint": "later.py", "pool": "xsmall"}).json()
    assert waiting["status"] == "queued"

    view = {w["name"]: w for w in client.get("/v1/compute/warehouses", headers=as_("ana")).json()}
    xsmall = view["xsmall"]
    assert (xsmall["in_use"], xsmall["max_concurrent"], xsmall["queued"]) == (2, 2, 1)
    assert sum(l["in_use"] for l in xsmall["layers"]) == 2 and sum(l["queued"] for l in xsmall["layers"]) == 1
    assert all(l["layer_name"] for l in xsmall["layers"])
    shown = {(o["category"], o["name"], o["state"]) for o in xsmall["occupants"]}
    assert ("job", "mine.py", "running") in shown and ("job", "later.py", "queued") in shown
    assert next(o for o in xsmall["occupants"] if o["name"] == "later.py")["queue_position"] == 1
    assert ("app", "Sales", "running") in shown  # ana deployed it, so she may see it
    assert mine["id"] in {o["run_id"] for o in xsmall["occupants"]}

    # Someone else sees the same counts, but none of ana's work in detail.
    for_bob = {w["name"]: w for w in client.get("/v1/compute/warehouses", headers=as_("bob")).json()}["xsmall"]
    assert (for_bob["in_use"], for_bob["queued"]) == (2, 1)
    assert not any(o["category"] == "job" for o in for_bob["occupants"])
    assert for_bob["others_queued"] == 1 and for_bob["others_in_use"] >= 1


def test_the_workspace_warehouse_lists_workspaces_without_counting_them(client):
    client.put("/v1/admin/settings/workspaces", headers=as_("admin"),
               json={"warehouse": "medium", "max_running": 4, "max_running_per_user": 2})
    start_workspace(client)
    medium = {w["name"]: w for w in client.get("/v1/compute/warehouses", headers=as_("ana")).json()}["medium"]
    assert medium["in_use"] == 0 and medium["workspaces"] == {"running": 1, "queued": 0, "max_running": 4}
    assert [o["category"] for o in medium["occupants"]] == ["workspace"] and medium["occupants"][0]["slots"] == 0


def test_a_run_the_cluster_cannot_place_yet_says_why_it_waits(client):
    ws = start_workspace(client)
    waiting = run(client, ws)
    orch = deps.get_orchestrator()
    orch.states["run-" + waiting["id"]] = "pending"
    orch.waiting["run-" + waiting["id"]] = "waiting for cluster capacity: no node has enough memory free"
    tick()
    shown = client.get(f"/v1/runs/{waiting['id']}", headers=as_("ana")).json()
    assert shown["status"] == "pending" and "no node has enough memory free" in shown["waiting_reason"]
    orch.states["run-" + waiting["id"]] = "running"
    orch.waiting.clear()
    tick()
    shown = client.get(f"/v1/runs/{waiting['id']}", headers=as_("ana")).json()
    assert (shown["status"], shown["waiting_reason"]) == ("running", None)


def test_a_run_whose_image_can_never_be_pulled_fails_with_the_reason(client):
    ws = start_workspace(client)
    broken = run(client, ws)
    orch = deps.get_orchestrator()
    orch.states["run-" + broken["id"]] = "pending"
    orch.waiting["run-" + broken["id"]] = "its image fdp/compute-python:dev cannot be pulled (ErrImageNeverPull)"
    tick()
    shown = client.get(f"/v1/runs/{broken['id']}", headers=as_("ana")).json()
    assert shown["status"] == "failed" and "ErrImageNeverPull" in shown["waiting_reason"]


def test_scheduler_messages_become_plain_reasons():
    from app.orchestrator.kubernetes import _scheduling
    message = ("0/1 nodes are available: 1 Insufficient memory. preemption: 0/1 nodes are available: "
               "1 No preemption victims found for incoming pod.")
    assert _scheduling(message) == "no node has enough memory free"
    assert _scheduling("0/3 nodes are available: 3 Insufficient cpu, 3 Insufficient memory.") == "no node has enough cpu and memory free"
