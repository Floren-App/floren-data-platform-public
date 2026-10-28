"""API traffic and delegated requests stay inside the layer that owns them."""
from sqlalchemy import select

from app import deps, layers, rbac
from app.auth import User
from app.config import get_settings
from app.db import SessionLocal
from app.environments import bind
from app.models import DataSource, LayerOperation, Resource, Run
from .conftest import as_
from .rbac_helpers import grant_group, integration
from .test_api import start_workspace, execution_token
from .test_apis import JWT, Backend, backend, internal_body, issuer  # noqa: F401
from .test_layer_infrastructure import managed
from .test_deploying import deploy, repo  # noqa: F401


def test_operation_must_run_a_program_deployed_in_the_apis_layer(client, repo):  # noqa: F811
    pub = deploy(client, repo, user="admin", program="explore", slug="explore").json()  # Non-Prod
    prod = client.post("/v1/apis", headers=as_("admin"), json={**internal_body(**JWT), "layer_id": layers.PROD}).json()
    op = {"name": "explore", "method": "POST", "path": "/explore", "internal": {"workload_id": pub["id"], "args": {}}}
    refused = client.post(f"/v1/apis/{prod['id']}/operations", headers=as_("admin"), json=op)
    assert refused.status_code == 422
    assert "this API's layer" in refused.json()["detail"]["errors"]["internal.workload_id"]
    same = client.post("/v1/apis", headers=as_("admin"), json={**internal_body(**JWT), "slug": "orders-np"}).json()
    assert client.post(f"/v1/apis/{same['id']}/operations", headers=as_("admin"), json=op).status_code == 201


def test_api_call_runs_inside_its_managed_layer_and_answers_from_its_artifacts(client, repo, issuer, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "layer_namespace_prefix", "dc-x-layer")
    pub = deploy(client, repo, user="admin", program="explore", slug="explore").json()
    path = "/v1/admin/layers/" + layers.NON_PROD + "/infrastructure"
    assert client.put(path, headers=as_("admin"), json={"expected_generation": 0, "spec": managed()}).status_code == 202
    with SessionLocal() as db:
        db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layers.NON_PROD)).phase = "Ready"
        db.commit()
    api = client.post("/v1/apis", headers=as_("admin"), json=internal_body(**JWT)).json()
    client.post(f"/v1/apis/{api['id']}/operations", headers=as_("admin"), json={
        "name": "explore", "method": "POST", "path": "/explore", "internal": {"workload_id": pub["id"], "args": {}}})
    client.put(f"/v1/rbac/resources/{pub['resource_id']}/assignments", headers=as_("admin"),
               json={"principal_id": api["identity"]["principal_id"], "role": "Executor"}).raise_for_status()
    caller = {"Authorization": f"Bearer {issuer(scope='orders.run')}"}
    started = client.post("/gw/orders/explore", headers=caller)
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    orch = deps.get_orchestrator()
    namespace = "dc-x-layer-" + layers.NON_PROD
    spec = orch.executions[f"run-{run_id}"]
    assert spec.placement.namespace == namespace
    assert {m.volume for m in spec.mounts} == {"fdp-artifacts"}
    # The program's reply is read from its own layer, never from the shared data volume.
    orch.write_artifact(namespace, f"runs/{run_id}/outputs/response.json", b'{"answer": 42}')
    orch.states[f"run-{run_id}"] = "succeeded"
    status = client.get(f"/gw/orders/_runs/{run_id}", headers=caller).json()
    assert status["status"] == "succeeded" and status["result"] == {"answer": 42}


def test_workloads_cannot_call_another_layers_external_api(client, backend):  # noqa: F811
    prod = client.post("/v1/apis", headers=as_("admin"), json={"name": "Partner", "slug": "partner", "direction": "external",
        "layer_id": layers.PROD, "outbound": {"base_url": backend}}).json()
    op = client.post(f"/v1/apis/{prod['id']}/operations", headers=as_("admin"), json={"name": "ping", "path": "/ping"}).json()
    grant_group(client, op["resource_id"], "Partner consumers", "Consumer", users=["bob"])
    assert client.get("/v1/egress/partner/ping", headers=as_("bob")).status_code == 200  # people may test it
    seen = len(Backend.seen)
    _, token = execution_token(start_workspace(client, "bob"))  # bob's workspace is in Non-Prod
    assert client.get("/v1/egress/partner/ping", headers=token).status_code in (403, 404)
    assert len(Backend.seen) == seen


def test_delegated_data_scope_never_includes_another_layers_source(client):
    client.put("/v1/admin/datasources/prod-files", headers=as_("admin"),
               json=integration("prod-files", "s3", layer_id=layers.PROD)).raise_for_status()
    schema = client.post("/v1/rbac/sources/prod-files/schemas", headers=as_("admin"), json={"name": "reports"}).json()
    grant_group(client, schema["id"], "Prod readers", "Reader", users=["ana"])
    with SessionLocal() as db:
        row = db.get(Resource, schema["id"])
        row.config = {**row.config, "state": "ready"}
        db.commit()
        source = db.scalar(select(DataSource).where(DataSource.name == "prod-files"))
        ana = rbac.resolve_user(db, User("ana"))
        assert rbac.data_scope(db, ana, source)  # interactively, Ana can read it
        nonprod_app = db.scalar(select(Resource).where(Resource.kind == "layer", Resource.id == layers.NON_PROD))
        viewer = rbac.resolve_user(db, bind(db, User("ana", principal_id=ana.principal_id), nonprod_app))
        assert viewer.environment_id == layers.NON_PROD
        assert not rbac.data_scope(db, viewer, source)
