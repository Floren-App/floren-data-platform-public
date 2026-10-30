import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="fdp-test-")
os.environ.update(
    FDP_DATABASE_URL=os.environ.get("FDP_TEST_CP_URL", "sqlite://"),
    FDP_ORCHESTRATOR="fake",
    FDP_SECRETS_BACKEND="memory",
    FDP_SESSION_SIGNING_KEY="test-signing-key-that-is-at-least-32-bytes",
    FDP_GATEWAY_CONFIG_TOKEN="test-gateway-config-token",
    FDP_ALLOW_FILE_REPOS="true",
    FDP_GIT_CACHE_DIR=os.path.join(_tmp, "git"),
    FDP_DATA_DIR=os.path.join(_tmp, "data"),
    FDP_PACKAGE_DIR=os.path.join(_tmp, "packages"),
    FDP_APP_START_TIMEOUT_SECONDS="3",   # a deploy that never becomes ready fails fast
    FDP_QUERY_EXECUTOR="native",  # legacy adapter coverage; shared SQL tests opt in explicitly
    FDP_API_PRIVATE_NETWORKS="127.0.0.0/8,::1/128",  # test backends run locally; tests of the default override it
)

import pytest  # noqa: E402
from fastapi import Header  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, deps  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

USERS = {
    "admin": auth.User("admin", roles=["fdp-admin"], groups=["platform"]),
    "ana": auth.User("ana", groups=["finance"]),
    "bob": auth.User("bob", groups=["marketing"]),
}


def fake_user(authorization: str = Header(default="")) -> auth.User:
    # Tests send "Bearer <username>" in place of a real OIDC token.
    return USERS[authorization.split(" ", 1)[1]]


class FakeVerifier:
    def verify(self, token: str) -> auth.User:
        return USERS[token]


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(engine)
    import shutil
    shutil.rmtree(os.path.join(_tmp, "packages"), ignore_errors=True)  # files of packages from earlier tests
    deps._orchestrator = None  # fresh fake orchestrator (and simulated volumes) per test
    monkeypatch.setattr(auth, "_verifier", FakeVerifier())
    app.dependency_overrides[auth.get_current_user] = fake_user
    with TestClient(app) as c:
        configure_layers()
        configure_warehouses()
        yield c
    app.dependency_overrides.clear()


# The platform creates no warehouses by itself; tests configure the sizes they use, as an administrator
# or OpenTofu would.
TEST_WAREHOUSES = [("xsmall", 1, 4, 300), ("small", 2, 8, 600), ("medium", 4, 16, 600),
                   ("large", 8, 32, 900), ("xlarge", 16, 64, 900),
                   ("trino", 1, 4, 600)]  # only the layers' Trino workers use it


def configure_warehouses():
    from app.db import SessionLocal
    from app.models import ComputePool
    with SessionLocal() as db:
        for name, cpu, memory, suspend in TEST_WAREHOUSES:
            db.add(ComputePool(name=name, cpu=cpu, memory_gb=memory, auto_suspend_seconds=suspend))
        db.commit()


def as_(user: str) -> dict:
    return {"Authorization": f"Bearer {user}"}


def configure_layers():
    """The platform creates no layers by itself; tests use a production layer and a default one, with the IDs
    earlier installations had for Prod and Non-Prod."""
    from app import layers
    from app.db import SessionLocal
    from app.models import PlatformSetting, Resource
    with SessionLocal() as db:
        db.add(Resource(id=layers.PROD, kind="layer", name="Prod", config={"workspaces": True, "description": "", "production": True}))
        db.add(Resource(id=layers.NON_PROD, kind="layer", name="Non-Prod", config={"workspaces": True, "description": "", "production": False}))
        db.add(PlatformSetting(key=layers.DEFAULTS, value={"default": layers.NON_PROD}))
        db.commit()
