"""The central workload log: platform entries as runs and deploys happen, program lines via fdp.log."""
from app.auth import mint_token
from app.db import SessionLocal
from app.models import Resource, Run, Workload
from .conftest import as_
from .test_api import start_workspace
from .test_deploying import deploy, push, reconcile, repo  # noqa: F401


def run_token(run_id):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        return {"Authorization": "Bearer " + mint_token("execution", run.owner, 60, run=run.id, ws=run.workspace_id or "",
                                                         pub=run.workload_id or "", sha=run.commit_sha or "")}


def logs(client, user="ana", **params):
    response = client.get("/v1/logs", headers=as_(user), params=params)
    assert response.status_code == 200, response.text
    return response.json()["entries"]


def test_runs_log_their_lifecycle_and_program_lines_carry_the_run(client):
    ws = start_workspace(client)
    run = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "pool": "small", "kind": "script",
                                                            "entrypoint": "jobs/etl.py"}).json()
    started = logs(client, run_id=run["id"])
    assert [e["event"] for e in started] == ["run.started"] and started[0]["message"] == "jobs/etl.py started"
    assert started[0]["level"] == "info" and started[0]["source"] == "platform" and started[0]["layer_id"]

    # fdp.log from inside the run: the platform stamps run, workspace and time; the program only sends lines.
    written = client.post("/v1/logs", headers=run_token(run["id"]), json={"entries": [
        {"level": "info", "message": "loaded 120 rows", "fields": {"table": "sales", "rows": 120}},
        {"level": "warning", "message": "3 rows without a region"},
        {"level": "error", "message": "upload failed", "fields": {"attempt": 2}}]})
    assert written.status_code == 202 and written.json() == {"accepted": 3, "dropped": 0}
    # A program cannot pick the run or workload it logs for.
    assert client.post("/v1/logs", headers=run_token(run["id"]), json={"entries": [
        {"message": "x", "run_id": "someone-else"}]}).status_code == 422
    # A person's own token is not an execution: people do not write the log.
    assert client.post("/v1/logs", headers=as_("ana"), json={"entries": [{"message": "x"}]}).status_code == 401

    entries = logs(client, run_id=run["id"])
    assert [e["message"] for e in entries[:3]] == ["upload failed", "3 rows without a region", "loaded 120 rows"]
    assert all(e["run_id"] == run["id"] and e["workspace_id"] == ws for e in entries)
    assert entries[2]["fields"] == {"table": "sales", "rows": 120} and entries[2]["source"] == "program"
    assert [e["message"] for e in logs(client, run_id=run["id"], level="warning")] == ["upload failed", "3 rows without a region"]
    assert [e["message"] for e in logs(client, run_id=run["id"], q="ROWS")] == ["3 rows without a region", "loaded 120 rows"]
    # Workspace runs are private to their owner.
    assert logs(client, user="bob", run_id=run["id"]) == []

    client.post(f"/v1/runs/{run['id']}/stop", headers=as_("ana")).raise_for_status()
    newest = logs(client, run_id=run["id"])[0]
    assert newest["event"] == "run.stopped" and newest["level"] == "warning"
    # A finished run's token can no longer write.
    assert client.post("/v1/logs", headers=run_token(run["id"]), json={"entries": [{"message": "late"}]}).status_code == 401


def test_workload_entries_follow_deploys_commits_and_folders(client, repo):  # noqa: F811
    pub = deploy(client, repo, program="sales-report", slug="report").json()
    deployed = logs(client, workload_id=pub["id"])
    assert deployed[0]["event"] == "workload.deployed" and deployed[0]["commit_sha"] == pub["deployed_sha"]
    with SessionLocal() as db:
        folder = db.get(Resource, db.get(Workload, pub["id"]).resource_id).parent_id
    run = client.post(f"/v1/workloads/{pub['id']}/runs", headers=as_("ana"), json={"args": {}})
    assert run.status_code in (200, 201), run.text
    run = run.json()
    client.post("/v1/logs", headers=run_token(run["id"]), json={"entries": [{"level": "info", "message": "report built"}]}).raise_for_status()
    line = next(e for e in logs(client, workload_id=pub["id"]) if e["message"] == "report built")
    # The code version is the commit the run came from, not something the program says.
    assert line["commit_sha"] == pub["deployed_sha"] and line["workload_id"] == pub["id"] and line["workload"] == pub["title"]
    # The joint log (all Workloads) contains it; a folder filter narrows it.
    assert any(e["message"] == "report built" for e in logs(client, folder_id="workloads"))
    assert folder and any(e["message"] == "report built" for e in logs(client, folder_id=folder))
    assert not any(e["message"] == "report built" for e in logs(client, folder_id="no-such-folder"))
    # A new commit shows up as its own entries.
    push(repo["work"], "Second version", lambda w: open(w / "analysis" / "sales_report.py", "a").write("\n# v2\n"))
    reconcile()
    events = [e["event"] for e in logs(client, workload_id=pub["id"])]
    assert "workload.update_found" in events and events.count("workload.deployed") == 2
