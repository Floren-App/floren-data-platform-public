"""Runtime versions, dbt, Airflow, secrets and service accounts."""

import json
import os
import shutil
from urllib.parse import urlsplit

import pytest

from app import deps
from app.config import get_settings

from .conftest import USERS, as_
from .test_api import setup_finance_db, start_workspace
from .test_programs import put_config
from .test_deploying import EXAMPLE, git, deploy, push, reconcile  # noqa: F401  (fixtures/helpers)
from .test_deploying import repo  # noqa: F401  (pytest fixture)

DBT = {"kind": "dbt", "entrypoint": "dbt/finance", "dbt": {"data_source": "finance-dw"}, "warehouse": "small"}
AIRFLOW = {"kind": "airflow", "entrypoint": "airflow", "warehouse": "small"}


def config(**programs):
    return {"version": 1, "warehouse": "small", "programs": programs}


def wl_headers(spec):
    return {"Authorization": f"Bearer {spec.env['FDP_SESSION_TOKEN']}"}


# --- runtime versions ------------------------------------------------------------------------------

def test_runtime_catalog_and_version_choice(client):
    setup_finance_db(client)
    cat = client.get("/v1/runtimes", headers=as_("ana")).json()
    assert [r["version"] for r in cat["python"]] == ["3.12"] and cat["python"][0]["is_default"]
    for v in ("3.11", "3.10"):
        r = client.put(f"/v1/admin/runtimes/python/{v}", headers=as_("admin"), json={"image": f"fdp/compute-python:{v}"})
        assert r.status_code == 200, r.text
    assert [r["version"] for r in client.get("/v1/runtimes", headers=as_("ana")).json()["python"]] == ["3.12", "3.11", "3.10"]
    assert client.put("/v1/admin/runtimes/python/3.9", headers=as_("ana"), json={"image": "x/y"}).status_code == 403

    ws = start_workspace(client)
    put_config(ws, config=config(old={"entrypoint": "a.py", "python": "3.10"}, new={"entrypoint": "b.py"},
                                 gone={"entrypoint": "c.py", "python": "2.7"}))
    orch = deps.get_orchestrator()
    old = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "program": "old"}).json()
    assert old["runtime_version"] == "3.10" and orch.executions[f"run-{old['id']}"].image == "fdp/compute-python:3.10"
    new = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "program": "new"}).json()
    assert new["runtime_version"] == "3.12"  # the default
    r = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "program": "gone"})
    assert r.status_code == 422 and "3.12, 3.11, 3.10" in r.json()["detail"]


def test_at_most_five_versions(client):
    for v in ("3.8", "3.9", "3.10", "3.11"):
        client.put(f"/v1/admin/runtimes/python/{v}", headers=as_("admin"), json={"image": "img"})
    r = client.put("/v1/admin/runtimes/python/3.13", headers=as_("admin"), json={"image": "img"})
    assert r.status_code == 409 and "at most 5" in r.json()["detail"]


# --- dbt -----------------------------------------------------------------------------------------

def test_dbt_in_workspace_builds_in_your_dev_schema(client):
    setup_finance_db(client)
    ws = start_workspace(client)
    put_config(ws, config=config(models=DBT))
    cfg = client.get(f"/v1/workspaces/{ws}/config", headers=as_("ana")).json()
    assert [a["name"] for a in cfg["config"]["programs"]["models"]["args"]] == ["command", "select", "exclude", "full_refresh", "vars"]

    run = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws, "program": "models", "args": {"command": "build", "select": "tag:daily"}}).json()
    spec = deps.get_orchestrator().executions[f"run-{run['id']}"]
    assert spec.image == "fdp/compute-dbt:1.9" and run["runtime"] == "dbt"
    assert spec.env["FDP_DBT_TARGET"] == "dev" and spec.env["FDP_DBT_SCHEMA"] == "dbt_ana"
    assert 'dbt build --target "$FDP_DBT_TARGET" --select tag:daily' in spec.command[2]
    assert "dbt docs generate --static" in spec.command[2]
    freshness=client.post('/v1/runs',headers=as_('ana'),json={'workspace_id':ws,'program':'models','args':{'command':'freshness'}})
    assert freshness.status_code==201,freshness.text
    freshness_spec=deps.get_orchestrator().executions['run-'+freshness.json()['id']]
    assert 'dbt source freshness --target "$FDP_DBT_TARGET"' in freshness_spec.command[2]
    # bob is not granted the data source the project builds in.
    ws_b = start_workspace(client, "bob")
    put_config(ws_b, owner="bob", config=config(models=DBT))
    assert client.post("/v1/runs", headers=as_("bob"), json={"workspace_id": ws_b, "program": "models"}).status_code == 403


# --- secrets -------------------------------------------------------------------------------------

def test_secrets_scopes_injection_and_masking(client):
    setup_finance_db(client)
    assert client.put("/v1/secrets/user/me/openai", headers=as_("ana"), json={"value": "sk-ana-123456"}).status_code == 200
    assert client.put("/v1/secrets/group/finance/smtp", headers=as_("ana"), json={"value": "smtp-pass-987"}).status_code == 200
    assert client.put("/v1/secrets/group/finance/x", headers=as_("bob"), json={"value": "v"}).status_code == 403
    listed = {s["ref"] for s in client.get("/v1/secrets", headers=as_("ana")).json()}
    assert listed == {"me/openai", "group:finance/smtp"}
    assert client.get("/v1/secrets", headers=as_("bob")).json() == []
    assert "sk-ana" not in json.dumps(client.get("/v1/secrets", headers=as_("ana")).json())

    ws = start_workspace(client)
    put_config(ws, config=config(job={"entrypoint": "job.py", "secrets": {"OPENAI_API_KEY": "me/openai",
                                                                          "SMTP_PASSWORD": "group:finance/smtp"}}))
    run = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "program": "job"}).json()
    spec = deps.get_orchestrator().executions[f"run-{run['id']}"]
    assert spec.secret_env == {"OPENAI_API_KEY": "sk-ana-123456", "SMTP_PASSWORD": "smtp-pass-987"}
    assert "sk-ana-123456" not in json.dumps(spec.env)

    # Values printed by the program are masked in its console.
    client.post(f"/v1/runs/{run['id']}/stop", headers=as_("ana"))
    vol = deps.get_orchestrator().volumes[deps.get_orchestrator().executions[f"ws-{ws}"].home_volume]
    vol[f"/workspace/.fdp/runs/{run['id']}/console.log"] = b"key=sk-ana-123456 pass=smtp-pass-987\n"
    assert client.get(f"/v1/runs/{run['id']}/logs", headers=as_("ana")).text == "key=*** pass=***\n"

    # Someone else's "me/" is their own; a missing or foreign secret stops the run before it starts.
    ws_b = start_workspace(client, "bob")
    put_config(ws_b, owner="bob", config=config(job={"entrypoint": "job.py", "secrets": {"S": "group:finance/smtp"}}))
    r = client.post("/v1/runs", headers=as_("bob"), json={"workspace_id": ws_b, "program": "job"})
    assert r.status_code == 403 and "may not use secret group:finance/smtp" in r.json()["detail"]


def make_certificate(cn="api.finance.test", issuer_key=None, issuer_name=None):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer_name or name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
            .sign(issuer_key or key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return cert, key, pem


def key_pem(key, password=None):
    from cryptography.hazmat.primitives import serialization
    enc = serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc).decode()


def test_certificate_secrets_validate_describe_inject_and_mask(client):
    import base64
    from cryptography.hazmat.primitives.serialization import pkcs12, BestAvailableEncryption
    setup_finance_db(client)
    ca_cert, ca_key, ca_pem = make_certificate("Finance Test CA")
    cert, key, cert_pem = make_certificate("api.finance.test", ca_key, ca_cert.subject)
    put = lambda name, body: client.put(f"/v1/secrets/user/me/{name}", headers=as_("ana"), json={"kind": "certificate", **body})

    # The key must belong to the certificate; encrypted keys need their password.
    _, other_key, _ = make_certificate()
    r = put("api", {"certificate": cert_pem, "private_key": key_pem(other_key)})
    assert r.status_code == 422 and "does not belong" in r.json()["detail"]["errors"]["private_key"]
    r = put("api", {"certificate": cert_pem, "private_key": key_pem(key, "pw-123")})
    assert r.status_code == 422 and "encrypted" in r.json()["detail"]["errors"]["private_key"]
    assert put("api", {"certificate": "not a cert"}).json()["detail"]["errors"]["certificate"]
    r = put("api", {"certificate": cert_pem, "private_key": key_pem(key, "pw-123"), "password": "pw-123", "ca_chain": ca_pem,
                    "description": "Finance API client"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kind"] == "certificate" and out["certificate"]["subject"] == "CN=api.finance.test"
    assert out["certificate"]["issuer"] == "CN=Finance Test CA" and out["certificate"]["subject_alt_names"] == ["api.finance.test"]
    assert out["certificate"]["has_private_key"] and out["certificate"]["chain_length"] == 1
    listed = json.dumps(client.get("/v1/secrets", headers=as_("ana")).json())
    assert "PRIVATE KEY" not in listed and "BEGIN CERTIFICATE" not in listed  # facts only, never material

    # PKCS#12 (.p12/.pfx) uploads are converted to the same bundle; the password is not stored.
    p12 = pkcs12.serialize_key_and_certificates(b"api", key, cert, [ca_cert], BestAvailableEncryption(b"p12-pass"))
    assert put("api-p12", {"pkcs12_base64": base64.b64encode(p12).decode(), "password": "wrong"}).status_code == 422
    r = put("api-p12", {"pkcs12_base64": base64.b64encode(p12).decode(), "password": "p12-pass"})
    assert r.status_code == 200 and r.json()["certificate"]["sha256_fingerprint"] == out["certificate"]["sha256_fingerprint"]

    # Programs receive VAR (certificate), VAR_KEY and VAR_CA; printed key material is masked.
    ws = start_workspace(client)
    put_config(ws, config=config(job={"entrypoint": "job.py", "secrets": {"FINANCE_API": "me/api"}}))
    run = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "program": "job"}).json()
    env = deps.get_orchestrator().executions[f"run-{run['id']}"].secret_env
    assert env["FINANCE_API"].startswith("-----BEGIN CERTIFICATE-----") and "PRIVATE KEY" in env["FINANCE_API_KEY"]
    assert env["FINANCE_API_CA"] == ca_pem
    client.post(f"/v1/runs/{run['id']}/stop", headers=as_("ana"))
    vol = deps.get_orchestrator().volumes[deps.get_orchestrator().executions[f"ws-{ws}"].home_volume]
    key_line = env["FINANCE_API_KEY"].splitlines()[1]
    vol[f"/workspace/.fdp/runs/{run['id']}/console.log"] = f"debug {key_line}\n".encode()
    assert key_line not in client.get(f"/v1/runs/{run['id']}/logs", headers=as_("ana")).text


# --- service accounts --------------------------------------------------------------------------

def make_sa(client, name="finance-etl"):
    r = client.put(f"/v1/admin/service-accounts/{name}", headers=as_("admin"), json={
        "description": "Finance pipelines", "groups": ["finance"], "allowed_groups": ["finance"]})
    assert r.status_code == 200, r.text
    from app import rbac
    from app.db import SessionLocal
    from app.models import Resource, DataSource
    from sqlalchemy import select
    from .rbac_helpers import grant_group
    with SessionLocal() as db:
        ident = rbac.resolve_user(db, __import__("app.auth", fromlist=["User"]).User(f"sa:{name}"))
        pid = ident.principal_id
        db.commit()
        schemas = list(db.scalars(select(Resource).where(Resource.kind == "schema")))
    for schema in schemas:
        grant_group(client, schema.id, "Finance services", "Admin", services=[pid])
    grant_group(client, "workloads", "Service executors", "Executor", services=[pid])
    from app.models import ServiceAccount
    with SessionLocal() as db:
        sa = db.scalar(select(ServiceAccount).where(ServiceAccount.name == name))
        resource = rbac.service_account_resource(db, sa)
        rid = resource.id
        db.commit()
    grant_group(client, rid, "Service managers", "Owner", users=["ana"])
    return r.json()


def test_service_tokens_authenticate_and_revoke(client, monkeypatch):
    from app import data_access
    monkeypatch.setattr(data_access, "credentials", lambda *args: ({"username": "svc_finance", "password": "s3cret"}, {}))
    setup_finance_db(client)
    make_sa(client)
    assert [s["name"] for s in client.get("/v1/service-accounts", headers=as_("ana")).json()] == ["finance-etl"]
    assert client.get("/v1/service-accounts", headers=as_("bob")).json() == []
    assert client.post("/v1/service-accounts/finance-etl/tokens", headers=as_("bob"), json={"name": "x"}).status_code == 403

    r = client.post("/v1/service-accounts/finance-etl/tokens", headers=as_("ana"),
                    json={"name": "external-bi", "expires_days": 90, "store_as_secret": "bi-token"})
    assert r.status_code == 201
    token = r.json()["token"]
    assert token.startswith("dcs_") and r.json()["stored_as"] == "sa:finance-etl/bi-token"
    assert all(t["token"] is None for t in client.get("/v1/service-accounts/finance-etl/tokens", headers=as_("ana")).json())

    # The token is the service account: it can read the data sources the account's groups are granted.
    bearer = {"Authorization": f"Bearer {token}"}
    creds = client.get("/v1/connect/finance-dw", headers=bearer)
    assert creds.status_code == 200 and creds.json()["credentials"]["username"] == "svc_finance"
    audit = [e for e in client.get("/v1/admin/audit", headers=as_("admin")).json() if e["action"] == "datasource.connect"]
    assert audit[0]["actor"] == "sa:finance-etl"

    tid = r.json()["id"]
    assert client.delete(f"/v1/service-accounts/finance-etl/tokens/{tid}", headers=as_("ana")).status_code == 204
    assert client.get("/v1/connect/finance-dw", headers=bearer).status_code == 401


# --- deploying dbt and Airflow -------------------------------------------------------------------

@pytest.fixture()
def platform_repo(repo):
    """The example repo (which has a dbt project and an Airflow deployment), with test data sources."""
    def add(w):
        cfg = json.loads((w / "fdp.json").read_text())
        cfg["programs"]["finance-models"] = DBT
        cfg["programs"]["pipelines"] = AIRFLOW
        (w / "fdp.json").write_text(json.dumps(cfg))
        git("add", ".", cwd=w)
    push(repo["work"], "Use the test data source", add)
    return repo


def fake_docs_job():
    """Simulates the dbt docs run: writes the docs into the workload's folder and succeeds."""
    def on_start(spec):
        if spec.name.startswith("run-") and "FDP_DOCS_DIR" in spec.env:
            sub = next(m.subpath for m in spec.mounts if m.target == "/fdp/docs")
            path = os.path.join(get_settings().data_dir, sub)
            os.makedirs(path, exist_ok=True)
            open(os.path.join(path, "index.html"), "w").write("<html>lineage</html>")
            return "succeeded"
        return None
    deps.get_orchestrator().on_start = on_start


def test_deploy_dbt_serves_docs_and_runs_in_prod(client, platform_repo):
    fake_docs_job()
    make_sa(client)
    pub = deploy(client, platform_repo, program="finance-models", slug="finance-models", run_as="finance-etl").json()
    assert pub["status"] == "live" and pub["identity"] == "sa:finance-etl" and pub["url"].endswith("/p/finance-models/")
    assert pub["events"][0]["kind"] == "deployed"

    # Docs are only for allowed viewers, through the ticket → cookie flow.
    url = client.post(f"/v1/workloads/{pub['id']}/open", headers=as_("bob")).json()["url"]
    u = urlsplit(url)
    r = client.get("/internal/auth/forward", headers={"X-Forwarded-Uri": f"{u.path}?{u.query}"}, follow_redirects=False)
    cookie = r.cookies["dc_p_finance-models"]
    docs = client.get("/p/finance-models/", cookies={"dc_p_finance-models": cookie})
    assert docs.status_code == 200 and "lineage" in docs.text
    assert client.get("/p/finance-models/").status_code == 401   # direct access without the cookie

    # A deployed run builds in the production schema and refreshes the docs.
    deps.get_orchestrator().on_start = None
    run = client.post(f"/v1/workloads/{pub['id']}/runs", headers=as_("ana"), json={"args": {"command": "build"}}).json()
    spec = deps.get_orchestrator().executions[f"run-{run['id']}"]
    assert spec.env["FDP_DBT_TARGET"] == "prod" and spec.env["FDP_DBT_SCHEMA"] == "analytics"
    assert spec.env["FDP_DOCS_DIR"] == "/fdp/docs"


def test_airflow_workload_orchestrates_programs(client, platform_repo):
    make_sa(client)
    client.put("/v1/secrets/sa/finance-etl/slack", headers=as_("ana"), json={"value": "xoxb-secret-1"})
    pub = deploy(client, platform_repo, program="pipelines", slug="pipelines", run_as="finance-etl",
                  secrets={"SLACK_TOKEN": "me/slack"}).json()
    assert pub["status"] == "live", pub
    orch = deps.get_orchestrator()
    spec = orch.executions[f"pub-{pub['id']}-{pub['deployed_sha'][:7]}"]
    assert spec.image == "fdp/airflow:dev" and spec.run_as_group == 0
    assert spec.secret_env == {"SLACK_TOKEN": "xoxb-secret-1"}    # "me" = the service account
    assert {m.target for m in spec.mounts} >= {"/fdp/airflow"}
    assert "airflow standalone" in spec.command[2] and "/p/pipelines" in spec.command[2]
    routes = client.get("/internal/traefik/config", headers={"X-FDP-Gateway-Config": "test-gateway-config-token"}).json()["http"]
    assert "p-pipelines-strip" not in routes["middlewares"]

    # A DAG task (with the deployment's token) runs a program from the same repo at the same commit…
    task = client.post("/v1/orchestrate/runs", headers=wl_headers(spec),
                       json={"program": "sales-report", "args": {"region": "North"}, "warehouse": "xsmall"})
    assert task.status_code == 201, task.text
    t = task.json()
    assert t["owner"] == "sa:finance-etl" and t["commit_sha"] == pub["deployed_sha"] and t["pool_name"] == "xsmall"
    # …which people allowed to use the service account can follow; others can't.
    assert client.get(f"/v1/runs/{t['id']}", headers=as_("ana")).status_code == 200
    assert client.get(f"/v1/runs/{t['id']}", headers=as_("bob")).status_code == 404

    # …or a deployed program by name.
    fake_docs_job()
    models = deploy(client, platform_repo, program="finance-models", slug="finance-models").json()
    deps.get_orchestrator().on_start = None
    task = client.post("/v1/orchestrate/runs", headers=wl_headers(spec),
                       json={"workload": "finance-models", "args": {"command": "run", "select": "tag:daily"}})
    assert task.status_code == 201 and task.json()["workload_id"] == models["id"]
    report = deploy(client, platform_repo, program="sales-report", slug="callable-report").json()
    assert report['execution_state'] == 'on_demand'
    task = client.post('/v1/orchestrate/runs', headers=wl_headers(spec),
                       json={'workload': 'callable-report', 'args': {'region': 'North'}})
    assert task.status_code == 201 and task.json()['workload_id'] == report['id'], task.text
    assert client.post("/v1/orchestrate/runs", headers=wl_headers(spec), json={"program": "nope"}).status_code == 404

    # GitOps for Airflow: recreate (old scheduler stopped before the new one starts).
    old = spec.name
    def new_dag(w):
        (w / "airflow" / "dags" / "weekly.py").write_text("# dag\n")
        git("add", ".", cwd=w)
    push(platform_repo["work"], "New DAG", new_dag)
    fake_docs_job()  # the dbt workload follows the same branch and regenerates its docs
    reconcile()
    assert old not in orch.executions


def test_airflow_test_deployment_from_workspace(client):
    setup_finance_db(client)
    ws = start_workspace(client)
    put_config(ws, config=config(pipelines=AIRFLOW, report={"entrypoint": "r.py"}))
    run = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws, "program": "pipelines"}).json()
    spec = deps.get_orchestrator().executions[f"run-{run['id']}"]
    assert spec.port == 8501 and f"/apps/{run['id']}" in spec.command[2]
    assert client.post(f"/v1/runs/{run['id']}/open", headers=as_("ana")).status_code == 200
    # Its DAG tasks run the workspace's programs.
    task = client.post("/v1/orchestrate/runs", headers=wl_headers(spec), json={"program": "report"})
    assert task.status_code == 201 and task.json()["workspace_id"] == ws


def test_run_as_requires_permission(client, repo):
    make_sa(client)
    from .rbac_helpers import grant_group
    grant_group(client, "workloads", "Bob deployer", "Admin", users=["bob"])
    r = deploy(client, repo, user="bob", program="sales-report", slug="bobs", run_as="finance-etl")
    assert r.status_code == 403 and "service account" in r.json()["detail"]


def test_distributed_airflow(client, platform_repo, monkeypatch):
    from app import airflow_db

    monkeypatch.setattr(airflow_db, "ensure_database",
                        lambda store, pub_id: f"postgresql+psycopg2://airflow_{pub_id}:pw@db:5432/airflow_{pub_id}")

    def distributed(w):
        cfg = json.loads((w / "fdp.json").read_text())
        cfg["programs"]["pipelines"]["airflow_settings"] = {"mode": "distributed", "workers": 3, "worker_concurrency": 4}
        (w / "fdp.json").write_text(json.dumps(cfg))
    push(platform_repo["work"], "Distributed Airflow", distributed)
    pub = deploy(client, platform_repo, program="pipelines", slug="pipelines").json()
    assert pub["status"] == "live", pub
    orch = deps.get_orchestrator()
    sha7 = pub["deployed_sha"][:7]
    names = sorted(n for n in orch.executions if n.startswith(f"pub-{pub['id']}"))
    assert names == sorted([f"pub-{pub['id']}-broker", f"pub-{pub['id']}-{sha7}-web", f"pub-{pub['id']}-{sha7}-scheduler",
                            *[f"pub-{pub['id']}-{sha7}-worker-{i}" for i in (1, 2, 3)]])
    web = orch.executions[f"pub-{pub['id']}-{sha7}-web"]
    worker = orch.executions[f"pub-{pub['id']}-{sha7}-worker-1"]
    assert web.port == 8501 and worker.port is None
    assert worker.env["FDP_AIRFLOW_ROLE"] == "worker" and worker.env["AIRFLOW__CORE__EXECUTOR"] == "CeleryExecutor"
    assert worker.env["AIRFLOW__CELERY__WORKER_CONCURRENCY"] == "4"
    assert worker.secret_env["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"].endswith(f"/airflow_{pub['id']}")
    assert web.secret_env["AIRFLOW__CORE__FERNET_KEY"] == worker.secret_env["AIRFLOW__CORE__FERNET_KEY"]
    assert {m.target for m in worker.mounts} >= {"/fdp/airflow-logs"}
    routes = client.get("/internal/traefik/config", headers={"X-FDP-Gateway-Config": "test-gateway-config-token"}).json()["http"]["services"]
    assert routes["p-pipelines"]["loadBalancer"]["servers"][0]["url"] == f"http://pub-{pub['id']}-{sha7}-web:8501"

    # Self-healing: a lost worker brings the deployment back.
    orch.stop(f"pub-{pub['id']}-{sha7}-worker-2")
    reconcile()
    assert f"pub-{pub['id']}-{sha7}-worker-2" in orch.executions

    # A new commit replaces web/scheduler/workers; the broker stays.
    def dag(w):
        (w / "airflow" / "dags" / "weekly.py").write_text("# dag\n")
        git("add", ".", cwd=w)
    push(platform_repo["work"], "Weekly DAG", dag)
    fake_docs_job()
    reconcile()
    new = client.get(f"/v1/workloads/{pub['id']}", headers=as_("ana")).json()["deployed_sha"][:7]
    left = sorted(n for n in orch.executions if n.startswith(f"pub-{pub['id']}"))
    assert all(sha7 not in n for n in left) and f"pub-{pub['id']}-broker" in left and f"pub-{pub['id']}-{new}-web" in left

    # Undeploying stops every component, the broker included.
    client.delete(f"/v1/workloads/{pub['id']}", headers=as_("ana"))
    assert not [n for n in orch.executions if n.startswith(f"pub-{pub['id']}")]


def test_distributed_airflow_needs_a_metadata_server(client, platform_repo):
    def distributed(w):
        cfg = json.loads((w / "fdp.json").read_text())
        cfg["programs"]["pipelines"]["airflow_settings"] = {"mode": "distributed"}
        (w / "fdp.json").write_text(json.dumps(cfg))
    push(platform_repo["work"], "Distributed Airflow", distributed)
    pub = deploy(client, platform_repo, program="pipelines", slug="pipelines").json()
    assert pub["status"] == "failed"
    full = client.get(f"/v1/workloads/{pub['id']}", headers=as_("ana")).json()
    assert "FDP_AIRFLOW_METADATA_URL" in full["sync_error"]


def test_installation_runtimes_follow_helm_and_configured_versions_are_left_alone(client, monkeypatch):
    from app.config import get_settings
    from app.db import SessionLocal
    from app.runtimes import sync_installation_runtimes
    admin = as_("admin")
    before = {r["kind"]: r for r in client.get("/v1/admin/runtimes", headers=admin).json() if r["is_default"]}
    assert before["python"]["installation"] and before["python"]["version"] == get_settings().default_python_version
    # Its image belongs to Helm: changing it here is refused, enabling and describing it is fine.
    r = client.put("/v1/admin/runtimes/python/" + before["python"]["version"], headers=admin,
                   json={"image": "other/python:1", "is_default": True})
    assert r.status_code == 409 and "Helm" in r.json()["detail"]
    assert client.delete("/v1/admin/runtimes/python/" + before["python"]["version"], headers=admin).status_code == 409
    assert client.put("/v1/admin/runtimes/python/3.11", headers=admin, json={"image": "team/python:3.11"}).status_code == 200
    # An upgrade with new images reaches the installation's runtime, not the added version.
    monkeypatch.setattr(get_settings(), "image_compute_python", "registry/compute-python:new")
    with SessionLocal() as db:
        sync_installation_runtimes(db, get_settings())
        db.commit()
    rows = {r["version"]: r for r in client.get("/v1/admin/runtimes", headers=admin).json() if r["kind"] == "python"}
    assert rows[before["python"]["version"]]["image"] == "registry/compute-python:new"
    assert rows["3.11"]["image"] == "team/python:3.11" and not rows["3.11"]["installation"]
    # Helm moves to a new version: it becomes the default; the old one stays as an ordinary version.
    monkeypatch.setattr(get_settings(), "default_python_version", "3.13")
    with SessionLocal() as db:
        sync_installation_runtimes(db, get_settings())
        db.commit()
    rows = {r["version"]: r for r in client.get("/v1/admin/runtimes", headers=admin).json() if r["kind"] == "python"}
    assert rows["3.13"]["is_default"] and rows["3.13"]["installation"]
    assert not rows[before["python"]["version"]]["installation"] and not rows[before["python"]["version"]]["is_default"]


def test_without_r_nothing_r_is_offered(client, monkeypatch):
    from app.config import get_settings
    from app.db import SessionLocal
    from app.runtimes import sync_installation_runtimes
    from .test_api import onboard_workspace_group
    monkeypatch.setattr(get_settings(), "r_enabled", False)
    with SessionLocal() as db:
        sync_installation_runtimes(db, get_settings())
        db.commit()
    assert client.get("/v1/me", headers=as_("ana")).json()["features"] == {"r": False}
    assert client.get("/v1/runtimes", headers=as_("ana")).json()["r"] == []
    onboard_workspace_group(client, "ana")
    r = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "r", "editor": "rstudio"})
    assert r.status_code == 422 and "r.enabled" in r.json()["detail"]
    assert client.post("/v1/workspaces", headers=as_("ana"), json={"name": "py", "editor": "vscode"}).status_code == 201
    assert client.put("/v1/admin/runtimes/r/4.3", headers=as_("admin"), json={"image": "x/r:4.3"}).status_code == 422
    assert client.post("/v1/packages/imports", headers=as_("admin"), json={"kind": "r", "requirement": "dplyr"}).status_code == 422
    assert client.post("/v1/packages/r/upload", headers=as_("admin"), files={"file": ("x_1.0.tar.gz", b"x")}).status_code == 422
    listing = client.get("/v1/packages", headers=as_("ana")).json()
    assert listing["kinds"] == ["python"] and listing["upstream"]["r"] is False
