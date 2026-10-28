"""Deploying from git and GitOps, against a real (local) git repository."""

import io
import json
import os
import shutil
import subprocess
import tarfile
from urllib.parse import urlsplit

import pytest

from app import deps, deploying
from app.config import get_settings
from app.db import SessionLocal
from app.models import Workload

from .conftest import as_
from .test_api import setup_finance_db

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "examples", "workspace")


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=Ana", "-c", "user.email=ana@example.test", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path, client):
    """A bare 'remote' with the example workspace on main, registered by the admin."""
    remote, work = tmp_path / "analytics.git", tmp_path / "work"
    git("init", "--bare", "-q", "-b", "main", str(remote), cwd=tmp_path)
    shutil.copytree(EXAMPLE, work)
    git("init", "-q", "-b", "main", cwd=work)
    git("add", ".", cwd=work)
    git("commit", "-qm", "First version", cwd=work)
    git("remote", "add", "origin", str(remote), cwd=work)
    git("push", "-q", "origin", "main", cwd=work)

    deploying.RUN_INLINE = True
    deploying.probe = lambda url: True
    setup_finance_db(client)
    r = client.put("/v1/admin/repos/analytics", headers=as_("admin"), json={
        "name": "analytics", "url": f"file://{remote}", "default_branch": "main",
        "secret": {"username": "bot", "token": "s3cret-token"}})
    assert r.status_code == 200, r.text
    assert r.json()["has_credentials"] and "token" not in r.text
    return {"id": r.json()["id"], "work": work}


def push(work, message, edit):
    edit(work)
    git("commit", "-qam", message, cwd=work)
    git("push", "-q", "origin", "main", cwd=work)


def deploy(client, repo, user="ana", **body):
    if body.get("run_as"):
        body.setdefault("execution_mode", "fixed")
    payload = {"repo_id": repo["id"], "branch": "main", **body}
    return client.post("/v1/workloads", headers=as_(user), json=payload)


def reconcile():
    deploying.reconcile_once(SessionLocal, deps.get_orchestrator(), deps.get_secret_store())


def test_pick_repo_branch_and_pull(client, repo):
    assert [r["name"] for r in client.get("/v1/repos", headers=as_("bob")).json()] == ["analytics"]
    branches = client.get(f"/v1/repos/{repo['id']}/branches", headers=as_("ana")).json()
    assert [b["name"] for b in branches] == ["main"]
    pulled = client.post(f"/v1/repos/{repo['id']}/pull?branch=main", headers=as_("ana")).json()
    assert pulled["commit"]["subject"] == "First version" and pulled["errors"] == []
    assert sorted(pulled["programs"]) == ["explore", "finance-models", "finance-pipelines", "lake-models",
                                          "lake-report", "load-lake", "region-summary", "sales-dashboard",
                                          "sales-report"]


def test_repository_can_be_restricted_to_an_application_security_group(client, repo):
    from .rbac_helpers import grant_group
    group_id = grant_group(client,'workloads','Pokemon engineers','Admin',users=('ana',))
    response = client.put('/v1/admin/repos/analytics',headers=as_('admin'),json={
        'name':'analytics','url':'file://'+str(repo['work'].parent/'analytics.git'),
        'allowed_groups':[group_id]})
    assert response.status_code == 200,response.text
    assert client.get('/v1/repos',headers=as_('bob')).json() == []
    assert client.get('/v1/repos',headers=as_('ana')).json()[0]['id'] == repo['id']
    assert client.get('/v1/repos/'+repo['id']+'/branches',headers=as_('bob')).status_code == 404
    assert client.get('/v1/repos/'+repo['id']+'/branches',headers=as_('ana')).status_code == 200


def test_deploy_app_and_viewers(client, repo):
    r = deploy(client, repo, program="sales-dashboard", slug="sales", title="Sales dashboard")
    assert r.status_code == 201, r.text
    pub = r.json()
    assert pub["status"] == "live" and pub["url"].endswith("/p/sales/") and pub["deployed_commit"]["subject"] == "First version"
    orch = deps.get_orchestrator()
    spec = orch.executions[f"pub-{pub['id']}-{pub['deployed_sha'][:7]}"]
    script = spec.command[2]
    assert f"/v1/workloads/{pub['id']}/source?sha={pub['deployed_sha']}" in script
    assert "--server.baseUrlPath=/p/sales" in script
    assert "fdp.floren.app/ds-finance-dw" not in spec.labels  # caller app has no fixed data credential path

    routes = client.get("/internal/traefik/config", headers={"X-FDP-Gateway-Config": "test-gateway-config-token"}).json()["http"]["routers"]
    assert routes["p-sales"]["rule"] == "PathPrefix(`/p/sales/`)"

    # Everyone with the app can open it (bob has no data grants of his own).
    url = client.post(f"/v1/workloads/{pub['id']}/open", headers=as_("bob")).json()["url"]
    u = urlsplit(url)
    r = client.get("/internal/auth/forward", headers={"X-Forwarded-Uri": f"{u.path}?{u.query}"}, follow_redirects=False)
    assert r.status_code == 302

    # Narrow it to finance: bob loses access immediately, also with the cookie he already has.
    cookie = r.cookies["dc_p_sales"]
    from app.models import AppGroup, RoleAssignment
    from sqlalchemy import select
    with SessionLocal() as db:
        group = db.scalar(select(AppGroup).where(AppGroup.name == "Deployed executors"))
    client.delete(f"/v1/rbac/resources/workloads/assignments/{group.id}", headers=as_("admin"))
    denied = client.get("/internal/auth/forward", headers={"X-Forwarded-Uri": "/p/sales/"}, cookies={"dc_p_sales": cookie})
    assert denied.status_code == 403
    assert client.post(f"/v1/workloads/{pub['id']}/open", headers=as_("bob")).status_code == 404
    assert [p["slug"] for p in client.get("/v1/workloads", headers=as_("bob")).json()] == []
    assert client.patch(f"/v1/workloads/{pub['id']}", headers=as_("bob"), json={"title": "x"}).status_code == 404


def test_fixed_app_does_not_copy_deployer_data_access(client, repo):
    r = deploy(client, repo, program="sales-dashboard", slug="fixed-sales", execution_mode="fixed")
    assert r.status_code == 201
    assert r.json()["status"] == "failed"
    pub = r.json()
    assert pub["sync_error"] == 'Service identity for "sales-dashboard" has no data access to "finance-dw".'
    assert "service:" not in pub["sync_error"]
    # The deployer has schema-level visibility but cannot assign its own missing data role.
    assert pub["access_issue"]["sources"][0]["schemas"][0]["name"] == "public"
    assert not pub["access_issue"]["sources"][0]["schemas"][0]["can_assign"]
    admin = client.get(f"/v1/workloads/{pub['id']}", headers=as_("admin")).json()
    source = admin["access_issue"]["sources"][0]
    assert source["resource_id"] and source["can_assign"]
    assert admin["access_issue"]["identity"]["principal_id"] == pub["service_principal_id"]
    assert admin["access_issue"]["can_manage_groups"]
    # Previously saved warnings are readable immediately, without forcing a redeployment.
    with SessionLocal() as db:
        stored = db.get(Workload, pub["id"])
        stored.sync_error = f"{stored.identity} is not granted finance-dw"
        stored.access_issue = None
        db.commit()
    legacy = client.get(f"/v1/workloads/{pub['id']}", headers=as_("admin")).json()
    assert legacy["sync_error"] == admin["sync_error"] and legacy["access_issue"] == admin["access_issue"]
    # Binding the service to a granted group repairs deployment; diagnostics then clear.
    from .rbac_helpers import grant_group
    grant_group(client, source["resource_id"], "Dashboard readers", "Reader", services=[pub["service_principal_id"]])
    repaired = client.post(f"/v1/workloads/{pub['id']}/sync", headers=as_("ana")).json()
    assert repaired["status"] == "live" and repaired["sync_error"] is None and repaired["access_issue"] is None


def test_deployed_program_runs_with_runners_access(client, repo):
    pub = deploy(client, repo, program="sales-report", slug="sales-report").json()
    assert pub["status"] == "live" and pub["program_def"]["args"][0]["name"] == "start_date"

    # bob may see it, but it needs finance-dw and runs with *his* access.
    r = client.post(f"/v1/workloads/{pub['id']}/runs", headers=as_("bob"), json={})
    assert r.status_code == 403 and "finance-dw" in r.json()["detail"]["message"]
    issue = r.json()["detail"]
    assert 'User "bob"' in issue["message"]
    assert issue["sources"][0]["resource_id"] is None and not issue["sources"][0]["schemas"]
    assert not issue["can_manage_groups"] and issue["identity"]["principal_id"] is None

    r = client.post(f"/v1/workloads/{pub['id']}/runs", headers=as_("ana"), json={"args": {"region": "North"}})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["workload_id"] == pub["id"] and run["commit_sha"] == pub["deployed_sha"] and run["workspace_id"] is None
    spec = deps.get_orchestrator().executions[f"run-{run['id']}"]
    assert spec.home_volume is None  # no workspace involved
    assert {(m.target, m.subpath) for m in spec.mounts} == {
        ("/fdp/run", f"runs/{run['id']}"), ("/fdp/envs", f"envs/{pub['id']}")}

    # The run's own token can download exactly this workload's source; nobody else's can.
    token = spec.env["FDP_SESSION_TOKEN"]
    src = client.get(f"/v1/workloads/{pub['id']}/source?sha={pub['deployed_sha']}",
                     headers={"Authorization": f"Bearer {token}"})
    assert src.status_code == 200
    names = tarfile.open(fileobj=io.BytesIO(src.content)).getnames()
    assert "analysis/sales_report.py" in names and "fdp.json" in names
    # After a control-plane restart the Git cache is empty; the source is fetched again on demand.
    import shutil
    shutil.rmtree(os.path.join(deploying.get_git().cache_dir, f"{repo['id']}.git"))
    again = client.get(f"/v1/workloads/{pub['id']}/source?sha={pub['deployed_sha']}",
                       headers={"Authorization": f"Bearer {token}"})
    assert again.status_code == 200 and again.content == src.content
    other = deploy(client, repo, program="explore", slug="explore").json()
    assert client.get(f"/v1/workloads/{other['id']}/source?sha={pub['deployed_sha']}",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 404

    # Console and outputs come from the run's folder on the data volume.
    base = os.path.join(get_settings().data_dir, "runs", run["id"])
    os.makedirs(os.path.join(base, "outputs"), exist_ok=True)
    open(os.path.join(base, "console.log"), "w").write("hello\n")
    open(os.path.join(base, "outputs", "report.csv"), "w").write("a,b\n1,2\n")
    client.post(f"/v1/runs/{run['id']}/stop", headers=as_("ana"))
    assert client.get(f"/v1/runs/{run['id']}/logs", headers=as_("ana")).text == "hello\n"
    assert client.get(f"/v1/runs/{run['id']}/outputs", headers=as_("ana")).json() == [{"path": "report.csv", "size": 8}]


def test_same_commit_restart_keeps_namespaced_app(client, repo, monkeypatch):
    from app import artifacts
    from app.orchestrator.base import Placement
    orch = deps.get_orchestrator()
    original_place, original_start = artifacts.place, orch.start

    def placed(db, spec, layer, backend):
        original_place(db, spec, layer, backend)
        spec.placement = Placement(namespace='test-layer')

    def start(spec):
        handle = original_start(spec)
        handle.ref = f'test-layer/{spec.name}'
        return handle

    monkeypatch.setattr(artifacts, 'place', placed)
    monkeypatch.setattr(orch, 'start', start)
    pub = deploy(client, repo, program='sales-dashboard', slug='sales').json()
    response = client.patch(f"/v1/workloads/{pub['id']}", headers=as_('ana'),
                            json={'warehouse':'small'})
    assert response.status_code == 200, response.text
    name = f"pub-{pub['id']}-{pub['deployed_sha'][:7]}"
    assert orch.status(f'test-layer/{name}') == 'running'


def test_gitops_follows_the_branch(client, repo):
    pub = deploy(client, repo, program="sales-dashboard", slug="sales").json()
    orch = deps.get_orchestrator()
    first = f"pub-{pub['id']}-{pub['deployed_sha'][:7]}"

    reconcile()  # nothing new
    assert client.get(f"/v1/workloads/{pub['id']}", headers=as_("ana")).json()["deployed_sha"] == pub["deployed_sha"]

    # 1. A new commit is deployed; the old version is stopped after the switch.
    push(repo["work"], "Bigger title", lambda w: open(w / "dashboards" / "sales.py", "a").write("\nst.caption('v2')\n"))
    reconcile()
    now = client.get(f"/v1/workloads/{pub['id']}", headers=as_("ana")).json()
    assert now["deployed_commit"]["subject"] == "Bigger title" and now["status"] == "live"
    assert first not in orch.executions and f"pub-{pub['id']}-{now['deployed_sha'][:7]}" in orch.executions

    # 2. A broken fdp.json is not deployed; the current version keeps serving.
    def break_config(w):
        cfg = json.loads((w / "fdp.json").read_text())
        cfg["programs"]["sales-dashboard"]["entrypoint"] = "../outside.py"
        (w / "fdp.json").write_text(json.dumps(cfg))
    push(repo["work"], "Oops", break_config)
    reconcile()
    after = client.get(f"/v1/workloads/{pub['id']}", headers=as_("ana")).json()
    assert after["deployed_sha"] == now["deployed_sha"] and after["status"] == "live"
    assert "entrypoint" in after["sync_error"] and after["events"][0]["kind"] == "deploy_failed"
    reconcile()  # the broken commit is not retried every tick
    assert client.get(f"/v1/workloads/{pub['id']}", headers=as_("ana")).json()["events"][0]["kind"] == "deploy_failed"

    # 3. Fixing it deploys again and clears the error.
    def fix_config(w):
        cfg = json.loads((w / "fdp.json").read_text())
        cfg["programs"]["sales-dashboard"]["entrypoint"] = "dashboards/sales.py"
        (w / "fdp.json").write_text(json.dumps(cfg))
    push(repo["work"], "Fix config", fix_config)
    reconcile()
    fixed = client.get(f"/v1/workloads/{pub['id']}", headers=as_("ana")).json()
    assert fixed["deployed_commit"]["subject"] == "Fix config" and fixed["sync_error"] is None

    # 4. Self-healing: a vanished app container is restarted at the deployed commit.
    orch.stop(f"pub-{pub['id']}-{fixed['deployed_sha'][:7]}")
    reconcile()
    assert f"pub-{pub['id']}-{fixed['deployed_sha'][:7]}" in orch.executions


def test_auto_update_off_and_undeploy(client, repo):
    pub = deploy(client, repo, program="sales-report", slug="report", auto_update=False).json()
    push(repo["work"], "Change", lambda w: open(w / "analysis" / "sales_report.py", "a").write("\n# v2\n"))
    reconcile()
    now = client.get(f"/v1/workloads/{pub['id']}", headers=as_("ana")).json()
    assert now["deployed_sha"] == pub["deployed_sha"] and now["latest_sha"] != pub["deployed_sha"]

    assert client.delete(f"/v1/workloads/{pub['id']}", headers=as_("bob")).status_code == 403
    assert client.delete(f"/v1/workloads/{pub['id']}", headers=as_("ana")).status_code == 204
    assert client.get("/v1/workloads", headers=as_("ana")).json() == []
    with SessionLocal() as db:
        assert db.get(Workload, pub["id"]).status == "stopped"
    # The slug is free again.
    assert deploy(client, repo, program="sales-report", slug="report").status_code == 201


def test_repo_credentials_never_leak(client, repo, tmp_path):
    client.put("/v1/admin/repos/broken", headers=as_("admin"), json={
        "name": "broken", "url": f"file://{tmp_path}/missing.git", "secret": {"token": "very-secret"}})
    rid = [r for r in client.get("/v1/repos", headers=as_("ana")).json() if r["name"] == "broken"][0]["id"]
    r = client.get(f"/v1/repos/{rid}/branches", headers=as_("ana"))
    assert r.status_code == 502 and "very-secret" not in r.text
    bad = client.put("/v1/admin/repos/x1", headers=as_("admin"), json={"name": "x1", "url": "ssh://git@host/x.git"})
    assert bad.status_code == 422


def test_settings_redeploy_refetches_a_cold_cache_without_promoting_a_new_branch_head(client, repo):
    pub = deploy(client,repo,program='sales-dashboard',slug='sales-dashboard').json()
    assert pub['status']=='live'
    pinned = pub['deployed_sha']
    def remove_from_new_head(work):
        path=work/'fdp.json'
        config=json.loads(path.read_text())
        del config['programs']['sales-dashboard']
        path.write_text(json.dumps(config))
    push(repo['work'],'Remove dashboard from newer branch head',remove_from_new_head)
    shutil.rmtree(os.path.join(deploying.get_git().cache_dir,repo['id']+'.git'))
    result=client.patch('/v1/workloads/'+pub['id'],headers=as_('ana'),json={'warehouse':pub['warehouse']})
    assert result.status_code==200,result.text
    current=client.get('/v1/workloads/'+pub['id'],headers=as_('ana')).json()
    assert current['status']=='live' and current['deployed_sha']==pinned
    assert current['events'][0]['kind']=='deployed',current['events'][0]
