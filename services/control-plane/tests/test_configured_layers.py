"""Layers come only from configuration: none are built in, one may be the default, and any may be production."""
from sqlalchemy import select, text

from app import layers
from app.db import SessionLocal
from app.models import PlatformSetting, Resource
from .conftest import as_
from .test_api import onboard_workspace_group


def test_the_platform_creates_no_layers_by_itself(monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth, deps
    from app.db import Base, engine
    from app.main import app
    from .conftest import FakeVerifier, fake_user
    Base.metadata.drop_all(engine)
    deps._orchestrator = None
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    app.dependency_overrides[auth.get_current_user] = fake_user
    try:
        with TestClient(app) as client:
            with SessionLocal() as db:
                assert db.scalar(select(Resource.id).where(Resource.kind == "layer")) is None
            assert client.get("/v1/layers", headers=as_("admin")).json() == []
            onboard_workspace_group(client, "ana")
            refused = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "main", "editor": "vscode"})
            assert refused.status_code == 422 and "no layers are configured yet" in refused.text
            created = client.post("/v1/admin/layers", headers=as_("admin"), json={"name": "Dev"}).json()
            only = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "main", "editor": "vscode"})
            assert only.status_code == 201 and only.json()["layer_id"] == created["id"]  # the only layer is the default
    finally:
        app.dependency_overrides.clear()


def test_with_several_layers_work_goes_to_the_default_or_must_choose(client):
    onboard_workspace_group(client, "ana")
    workspace = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "main", "editor": "vscode"}).json()
    assert workspace["layer_id"] == layers.NON_PROD
    assert client.delete("/v1/admin/settings/default-layer", headers=as_("admin")).status_code == 204
    refused = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "second", "editor": "vscode"})
    assert refused.status_code == 422 and "choose a layer" in refused.text
    test = client.post("/v1/admin/layers", headers=as_("admin"), json={"name": "Test"}).json()
    chosen = client.put("/v1/admin/settings/default-layer", headers=as_("admin"), json={"layer_id": test["id"]}).json()
    assert chosen == {"layer_id": test["id"], "configured": True}
    assert client.put("/v1/admin/settings/default-layer", headers=as_("ana"), json={"layer_id": test["id"]}).status_code == 403
    listed = {layer["name"]: layer for layer in client.get("/v1/layers", headers=as_("ana")).json()}
    assert listed["Test"]["default"] and not listed["Non-Prod"]["default"] and listed["Prod"]["production"]


def test_the_default_layer_cannot_be_deleted_but_a_former_initial_layer_can(client):
    refused = client.delete(f"/v1/admin/layers/{layers.NON_PROD}", headers=as_("admin"))
    assert refused.status_code == 409 and "default layer" in refused.text
    assert client.delete(f"/v1/admin/layers/{layers.PROD}", headers=as_("admin")).status_code == 204


def test_personal_secrets_stay_out_of_production_layers_only(client):
    from app.auth import User
    from app.identity import can_access
    dev = client.post("/v1/admin/layers", headers=as_("admin"), json={"name": "Dev"}).json()
    live = client.post("/v1/admin/layers", headers=as_("admin"), json={"name": "Live", "production": True}).json()
    with SessionLocal() as db:
        def allowed(layer):
            return can_access(db, "user", "ana", User("ana", groups=["finance"], environment_id=layer))
        assert allowed(dev["id"]) and allowed(layers.NON_PROD)
        assert not allowed(live["id"]) and not allowed(layers.PROD)


def test_installs_with_built_in_layers_keep_them_as_ordinary_ones(client):
    with SessionLocal() as db:
        for ident in (layers.PROD, layers.NON_PROD):
            layer = db.get(Resource, ident)
            layer.config = {"workspaces": True, "description": "", "initial_layer": True}
        db.delete(db.get(PlatformSetting, layers.DEFAULTS))
        db.execute(text("DELETE FROM fdp_migrations WHERE revision='003_configured_layers'"))
        db.commit()
        layers.adopt_initial_layers(db)
        db.commit()
        prod, non_prod = db.get(Resource, layers.PROD), db.get(Resource, layers.NON_PROD)
        assert prod.config["production"] is True and non_prod.config["production"] is False
        assert "initial_layer" not in prod.config and layers.default_layer(db) == layers.NON_PROD
