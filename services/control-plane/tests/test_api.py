from urllib.parse import urlsplit

from app import deps
from app.models import Workspace
from app.db import SessionLocal

from .conftest import as_


def setup_finance_db(client):
    r = client.put("/v1/admin/datasources/finance-dw", headers=as_("admin"), json={
        "name": "finance-dw", "kind": "postgres",
        "config": {"host": "10.0.0.5", "port": 5432, "database": "finance", "catalog": "finance_dw"},
        "provisioner_secret": {"username": "governance_admin", "password": "governance"},
        "egress": ["10.0.0.5/32"],
        "secret": {"username": "svc_finance", "password": "s3cret"},
    })
    assert r.status_code == 200, r.text
    assert "secret" not in r.json()
    from .rbac_helpers import grant_group
    # Current authorization uses stable directory identities and schema roles.
    client.get("/v1/me", headers=as_("ana"))
    schema = client.post("/v1/rbac/sources/finance-dw/schemas", headers=as_("admin"), json={"name": "public"})
    assert schema.status_code == 201, schema.text
    grant_group(client, schema.json()["id"], "Finance data", "Reader", external_groups=["finance"])
    grant_group(client, "workloads", "Finance data", "Owner", external_groups=["finance"])
    grant_group(client, "workloads", "Deployed executors", "Executor", users=["bob"])
    assert deps.get_orchestrator().egress["finance-dw"] == ["10.0.0.5/32"]


def start_workspace(client, user="ana", editor="vscode"):
    onboard_workspace_group(client,user)
    response = client.post("/v1/workspaces", headers=as_(user), json={"name": "main", "editor": editor})
    assert response.status_code == 201,response.text
    ws = response.json()
    r = client.post(f"/v1/workspaces/{ws['id']}/start", headers=as_(user))
    assert r.json()["status"] == "running"
    return ws["id"]


def onboard_workspace_group(client,user):
    """Explicit admin onboarding, shared across fixture workspaces; the app creates no groups."""
    me = client.get('/v1/me',headers=as_(user)).json()
    if me.get('available_groups'):
        return
    admin = client.get('/v1/me',headers=as_('admin')).json()
    groups = client.get('/v1/rbac/groups',headers=as_('admin')).json()
    group = next((g for g in groups if g['name']=='Workspace developers'),None)
    if group is None:
        response = client.post('/v1/rbac/groups',headers=as_('admin'),json={
            'name':'Workspace developers','owners':[admin['principal_id']]})
        assert response.status_code == 201,response.text
        group = response.json()
    response = client.post('/v1/rbac/groups/'+group['id']+'/bindings',headers=as_('admin'),json={'principal_id':me['principal_id']})
    assert response.status_code == 201,response.text


def execution_token(ws_id):
    spec = deps.get_orchestrator().executions[f"ws-{ws_id}"]
    return spec, {"Authorization": f"Bearer {spec.env['FDP_SESSION_TOKEN']}"}


def test_non_admin_cannot_manage_catalog(client):
    r = client.put("/v1/admin/datasources/x", headers=as_("ana"), json={"name": "xx", "kind": "postgres"})
    assert r.status_code == 403


def test_default_pools_and_me(client):
    setup_finance_db(client)
    me = client.get("/v1/me", headers=as_("ana")).json()
    assert [p["name"] for p in me["pools"] if p["name"] != "trino"] == ["xsmall", "small", "medium", "large", "xlarge"]
    assert [d["name"] for d in me["data_sources"]] == ["finance-dw"]
    assert client.get("/v1/me", headers=as_("bob")).json()["data_sources"] == []


def test_workspace_is_tiny_and_locked_down(client):
    setup_finance_db(client)
    ws_id = start_workspace(client)
    spec, _ = execution_token(ws_id)
    assert spec.cpu == 0.5 and spec.memory_gb == 1.0
    assert "--disable-file-downloads" in spec.command
    assert spec.labels["fdp.floren.app/ds-finance-dw"] == "true"
    assert spec.home_volume == f"fdp-home-ws-{ws_id}"


def test_rstudio_workspace_routes_with_root_path(client):
    ws_id = start_workspace(client, editor="rstudio")
    spec, _ = execution_token(ws_id)
    assert spec.port == 8787 and spec.env["DISABLE_AUTH"] == "true"
    mws = client.get("/internal/traefik/config", headers={"X-FDP-Gateway-Config": "test-gateway-config-token"}).json()["http"]["middlewares"]
    assert mws[f"ws-{ws_id}-root"]["headers"]["customRequestHeaders"]["X-RStudio-Root-Path"] == f"/ws/{ws_id}"


def test_connect_respects_grants(client, monkeypatch):
    from app import data_access
    monkeypatch.setattr(data_access, "credentials", lambda *args: ({"username": "svc_finance", "password": "s3cret"}, {}))
    setup_finance_db(client)
    _, ana = execution_token(start_workspace(client, "ana"))
    r = client.get("/v1/connect/finance-dw", headers=ana)
    assert r.status_code == 200
    assert r.json()["credentials"] == {"username": "svc_finance", "password": "s3cret"}

    _, bob = execution_token(start_workspace(client, "bob"))
    assert client.get("/v1/connect/finance-dw", headers=bob).status_code == 404

    actions = [e["action"] for e in client.get("/v1/admin/audit", headers=as_("admin")).json()]
    assert "datasource.connect" in actions and "datasource.denied" in actions


def test_run_on_pool_from_workspace_terminal(client):
    ws_id = start_workspace(client)
    _, wl = execution_token(ws_id)
    r = client.post("/v1/runs", headers=wl, json={
        "workspace_id": ws_id, "pool": "large", "kind": "script", "entrypoint": "models/train.py"})
    assert r.status_code == 201, r.text
    spec = deps.get_orchestrator().executions[f"run-{r.json()['id']}"]
    assert (spec.cpu, spec.memory_gb) == (8, 32)
    assert spec.command[:2] == ["bash", "-c"] and "python models/train.py" in spec.command[2]


def test_entrypoint_cannot_escape_workspace(client):
    ws_id = start_workspace(client)
    r = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws_id, "pool": "small", "kind": "script", "entrypoint": "../../etc/passwd.py"})
    assert r.status_code == 422


def test_shiny_and_streamlit_apps(client):
    ws_id = start_workspace(client)
    orch = deps.get_orchestrator()
    st = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws_id, "pool": "small", "kind": "app", "entrypoint": "dash/app.py"}).json()
    sh = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws_id, "pool": "small", "kind": "app", "entrypoint": "shiny/sales"}).json()
    assert f"streamlit run dash/app.py" in orch.executions[f"run-{st['id']}"].command[2]
    assert f"--server.baseUrlPath=/apps/{st['id']}" in orch.executions[f"run-{st['id']}"].command[2]
    assert "shiny::runApp" in orch.executions[f"run-{sh['id']}"].command[2]

    cfg = client.get("/internal/traefik/config", headers={"X-FDP-Gateway-Config": "test-gateway-config-token"}).json()["http"]
    assert f"apps-{st['id']}" in cfg["routers"] and f"apps-{sh['id']}-strip" in cfg["middlewares"]
    assert f"apps-{st['id']}-strip" not in cfg["middlewares"]


def test_gateway_ticket_then_cookie(client):
    ws_id = start_workspace(client)
    url = client.post(f"/v1/workspaces/{ws_id}/open", headers=as_("ana")).json()["url"]
    uri = urlsplit(url)
    fwd = {"X-Forwarded-Uri": f"{uri.path}?{uri.query}",
           "X-Forwarded-Proto": "https", "X-Forwarded-Host": "localhost:8090"}

    r = client.get("/internal/auth/forward", headers=fwd, follow_redirects=False)
    # Absolute, on the host the browser used — never the control plane's internal address.
    assert r.status_code == 302 and r.headers["location"] == f"https://localhost:8090/ws/{ws_id}/"
    cookie = r.cookies[f"dc_ws_{ws_id}"]

    ok = client.get("/internal/auth/forward", headers={"X-Forwarded-Uri": f"/ws/{ws_id}/x"},
                    cookies={f"dc_ws_{ws_id}": cookie})
    assert ok.status_code == 200

    # A cookie for one workspace never opens someone else's.
    other = start_workspace(client, "bob")
    denied = client.get("/internal/auth/forward", headers={"X-Forwarded-Uri": f"/ws/{other}/"},
                        cookies={f"dc_ws_{other}": cookie})
    assert denied.status_code == 403


def test_at_most_two_running_workspaces_per_user(client):
    first, second = start_workspace(client), start_workspace(client)
    third = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "third", "editor": "rstudio"}).json()
    r = client.post(f"/v1/workspaces/{third['id']}/start", headers=as_("ana"))
    assert r.status_code == 409 and "2 workspaces running" in r.json()["detail"]
    # Starting one that already runs is fine; other users are unaffected; stopping frees a slot.
    assert client.post(f"/v1/workspaces/{first}/start", headers=as_("ana")).status_code == 200
    start_workspace(client, "bob")
    client.post(f"/v1/workspaces/{second}/stop", headers=as_("ana"))
    assert client.post(f"/v1/workspaces/{third['id']}/start", headers=as_("ana")).json()["status"] == "running"


def test_idle_workspace_is_suspended(client):
    from datetime import timedelta

    from app.config import get_settings
    from app.models import utcnow
    from app.reaper import reap_once

    ws_id = start_workspace(client)
    with SessionLocal() as db:
        db.get(Workspace, ws_id).last_activity_at = utcnow() - timedelta(hours=2)
        db.commit()
        reap_once(db, deps.get_orchestrator(), get_settings())
        assert db.get(Workspace, ws_id).status == "stopped"


def test_dead_workspace_container_is_detected_and_restartable(client):
    from app.config import get_settings
    from app.models import Workspace
    from app.reaper import reap_once

    ws_id = start_workspace(client)
    orch = deps.get_orchestrator()
    orch.states[f"ws-{ws_id}"] = "succeeded"           # exited, e.g. after a Docker restart
    r = client.post(f"/v1/workspaces/{ws_id}/start", headers=as_("ana")).json()
    assert r["status"] == "running" and orch.states[f"ws-{ws_id}"] == "running"
    orch.states[f"ws-{ws_id}"] = "failed"
    with SessionLocal() as db:
        reap_once(db, orch, get_settings())
        assert db.get(Workspace, ws_id).status == "failed"


def test_storage_integrations_are_typed_and_require_their_location(client):
    from .rbac_helpers import integration
    types = {t['kind']: t for t in client.get('/v1/admin/storage-types', headers=as_('admin')).json()}
    assert set(types) == {'postgres', 's3', 'iceberg'}
    assert {f['name'] for f in types['s3']['config'] if f['required']} == {'bucket', 'region', 'role_arn'}
    # Unsupported kinds and incomplete locations are refused with per-field errors.
    assert client.put('/v1/admin/datasources/old', headers=as_('admin'), json={'name': 'old', 'kind': 'snowflake'}).status_code == 422
    r = client.put('/v1/admin/datasources/files', headers=as_('admin'), json={'name': 'files', 'kind': 's3', 'config': {'bucket': 'Bad/Bucket'}})
    assert r.status_code == 422
    assert r.json()['detail']['errors'] == {'bucket': 'enter a literal bucket name', 'role_arn': 'required',
        'provisioner_secret.access_key_id': 'required', 'provisioner_secret.secret_access_key': 'required'}
    created = client.put('/v1/admin/datasources/files', headers=as_('admin'), json=integration('files', 's3', prefix='finance'))
    assert created.status_code == 200, created.text
    assert created.json()['config'] == {'bucket': 'files', 'prefix': 'finance', 'region': 'us-east-1',
                                        'role_arn': 'arn:aws:iam::123456789012:role/fdp', 'layer_id': '1a0000000002'}
    # Editing keeps stored credentials unless new ones are given; the type is fixed.
    edit = integration('files', 's3', prefix='finance/v2')
    edit.pop('provisioner_secret')
    assert client.put('/v1/admin/datasources/files', headers=as_('admin'), json=edit).status_code == 200
    assert client.put('/v1/admin/datasources/files', headers=as_('admin'), json=integration('files', 'postgres')).status_code == 409
    # An Iceberg warehouse cannot reuse a bucket registered for plain files.
    clash = client.put('/v1/admin/datasources/lake', headers=as_('admin'), json=integration('lake', 'iceberg', warehouse='s3://files/tables'))
    assert clash.status_code == 409 and 'warehouse' in clash.json()['detail']['errors']


def test_sql_workspace_runs_cloudbeaver_and_keeps_files_on_the_server(client):
    onboard_workspace_group(client,'ana')
    from app.config import get_settings
    from app.models import Workspace
    from app.executions import workspace_spec
    from app.db import SessionLocal
    ws = client.post('/v1/workspaces', headers=as_('ana'), json={'name': 'queries', 'editor': 'sql'})
    assert ws.status_code == 201, ws.text
    assert client.post('/v1/workspaces', headers=as_('ana'), json={'name': 'x', 'editor': 'datagrip'}).status_code == 422
    with SessionLocal() as db:
        row = db.get(Workspace, ws.json()['id'])
        spec = workspace_spec(get_settings(), row, [], [])
        row.status, row.upstream_url = 'running', 'http://ws-sql:8978'
        db.commit()
    assert spec.image == get_settings().image_workspace_sql and spec.port == 8978
    assert spec.run_as_user is None  # root volume bootstrap; the image drops the editor/proxy to uid 1000
    # It reaches data only through the SQL gateway, with this workspace's own token.
    assert spec.env['FDP_SESSION_TOKEN'] and spec.env['FDP_WORKSPACE_ID'] == ws.json()['id']
    assert spec.env['FDP_SQL_USER']=='fdp-'+ws.json().get('principal_id',row.principal_id)
    http = client.get("/internal/traefik/config", headers={"X-FDP-Gateway-Config": "test-gateway-config-token"}).json()['http']
    key = f"ws-{ws.json()['id']}"
    assert http['routers'][key]['middlewares'][0]=='sql-sanitize'
    assert all(v=='' for v in http['middlewares']['sql-sanitize']['headers']['customRequestHeaders'].values())
    assert f"{key}-strip" not in http['middlewares']  # CloudBeaver serves under /ws/<id>/ itself
    transfer = http['routers'][f'{key}-transfer']
    assert transfer['priority'] > 1000 and 'deny-all' in transfer['middlewares']
    for path in ('api/data/', 'api/resultset/blob/', 'api/fs-data/'):
        assert f"/ws/{ws.json()['id']}/{path}" in transfer['rule']
    assert http['middlewares']['deny-all'] == {'ipAllowList': {'sourceRange': ['127.0.0.2/32']}}
    from app.auth import mint_token
    cookie=mint_token('gateway','ana',60,target='ws:'+ws.json()['id'],pid=row.principal_id)
    response=client.get('/internal/auth/forward',headers={'X-Forwarded-Uri':'/ws/'+ws.json()['id']+'/',
        'Cookie':'dc_ws_'+ws.json()['id']+'='+cookie,'X-Role':'admin','X-FDP-Team':'admin'})
    assert response.status_code==200,response.text
    assert response.headers['x-fdp-user']==spec.env['FDP_SQL_USER']
    assert response.headers['x-fdp-team']=='user' and response.headers['x-fdp-display-name']=='ana'


def test_the_platform_creates_no_warehouses_by_itself(monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth, deps
    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.models import ComputePool
    from .conftest import FakeVerifier
    Base.metadata.drop_all(engine)
    deps._orchestrator = None
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    with TestClient(app):
        with SessionLocal() as db:
            assert db.query(ComputePool).count() == 0


def test_public_api_routes_are_rate_limited_per_credential(client, monkeypatch):
    from app.config import get_settings
    def config():
        return client.get("/internal/traefik/config", headers={"X-FDP-Gateway-Config": "test-gateway-config-token"}).json()["http"]
    http = config()
    assert http["middlewares"]["rate-limit"]["rateLimit"] == {"average": 100, "burst": 200, "period": "1s",
                                                             "sourceCriterion": {"requestHeaderName": "Authorization"}}
    for name in ("api", "sql", "gw"):
        assert http["routers"][name]["middlewares"][0] == "rate-limit", name
    assert "rate-limit" not in http["routers"].get("web", {}).get("middlewares", [])
    monkeypatch.setattr(get_settings(), "rate_limit_average", 0)
    http = config()
    assert "rate-limit" not in http["middlewares"] and "rate-limit" not in http["routers"]["api"]["middlewares"]
