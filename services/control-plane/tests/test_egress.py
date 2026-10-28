"""Workspace internet access: HTTPS to administrator-allowed host names only, through the egress proxy."""
import asyncio
import socket

from app import egress_proxy, layers
from app.egress_proxy import Proxy, allowed, public_addresses
from app.layer_infrastructure import InfrastructureSpec
from app.layer_manifests import ControllerConfig, WorkerPlan, manifests
from .conftest import as_
from .test_api import onboard_workspace_group
from .test_layer_infrastructure import managed

ADMIN = as_("admin")


def test_host_patterns_match_exact_names_and_subdomains():
    patterns = ["github.com", "*.githubusercontent.com"]
    assert allowed("github.com", patterns) and allowed("GitHub.com.", patterns)
    assert allowed("raw.githubusercontent.com", patterns) and allowed("a.b.githubusercontent.com", patterns)
    assert not allowed("githubusercontent.com", patterns)        # a wildcard covers subdomains only
    assert not allowed("api.github.com", patterns)
    assert not allowed("evilgithub.com", patterns) and not allowed("github.com.evil.io", patterns)
    assert not allowed("x.evilgithubusercontent.com", patterns)


def test_names_resolving_to_private_addresses_are_refused(monkeypatch):
    def resolve(addresses):
        return lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]
    monkeypatch.setattr(socket, "getaddrinfo", resolve(["140.82.121.3"]))
    assert public_addresses("github.com") == ["140.82.121.3"]
    for private in ("10.96.0.1", "192.0.2.5", "127.0.0.1", "169.254.169.254", "100.80.35.101"):
        monkeypatch.setattr(socket, "getaddrinfo", resolve(["140.82.121.3", private]))
        assert public_addresses("github.com") == [], private
    proxy = Proxy("http://unused")
    proxy.patterns = ["github.com"]
    assert proxy.check("github.com", "22").startswith("refused:")       # HTTPS only
    assert proxy.check("example.com", "443").startswith("refused:")
    assert proxy.check("github.com", "443").startswith("refused:")      # still resolves to a private address


def test_proxy_tunnels_allowed_hosts_and_refuses_the_rest(monkeypatch):
    async def scenario():
        async def echo(reader, writer):
            writer.write(b"upstream:" + await reader.read(100))
            await writer.drain()
            writer.close()
        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        monkeypatch.setattr(egress_proxy, "PORT", upstream.sockets[0].getsockname()[1])
        monkeypatch.setattr(egress_proxy, "public_addresses", lambda host: ["127.0.0.1"])
        proxy = Proxy("http://unused")
        proxy.patterns = ["github.com"]
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def connect(target):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            await writer.drain()
            status = await reader.readuntil(b"\r\n\r\n")
            return reader, writer, status.split(b"\r\n")[0]

        reader, writer, status = await connect(f"github.com:{egress_proxy.PORT}")
        assert status == b"HTTP/1.1 200 Connection Established"
        writer.write(b"hello")
        await writer.drain()
        assert await reader.read(100) == b"upstream:hello"
        writer.close()
        for target in (f"example.com:{egress_proxy.PORT}", "github.com:22"):
            _, writer, status = await connect(target)
            assert status == b"HTTP/1.1 403 Forbidden", target
            writer.close()
        server.close()
        upstream.close()
    asyncio.run(scenario())


def test_administrators_manage_the_allowlist_and_the_proxy_reads_it(client):
    path = "/v1/admin/settings/egress"
    assert client.get(path, headers=as_("ana")).status_code == 403
    assert client.get(path, headers=ADMIN).json()["hosts"] == []
    assert client.get("/internal/egress/allowlist").json() == {"hosts": []}   # empty: no internet access
    body = {"hosts": [{"host": "GitHub.com", "description": "Git"}, {"host": "*.githubusercontent.com"}]}
    r = client.put(path, headers=ADMIN, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["hosts"] == [{"host": "github.com", "description": "Git"},
                                 {"host": "*.githubusercontent.com", "description": ""}]
    assert client.get("/internal/egress/allowlist").json() == {"hosts": [{"host": "github.com"}, {"host": "*.githubusercontent.com"}]}
    for bad in ("*", "*.com", "10.0.0.1", "https://github.com", "github.com/path", "git hub.com", "*.*.example.com"):
        assert client.put(path, headers=ADMIN, json={"hosts": [{"host": bad}]}).status_code == 422, bad
    assert client.put(path, headers=ADMIN, json={"hosts": [{"host": "a.io"}, {"host": "A.io"}]}).status_code == 422
    assert client.put(path, headers=as_("ana"), json={"hosts": []}).status_code == 403
    assert client.put(path, headers=ADMIN, json={"hosts": []}).json()["hosts"] == []


def test_vscode_and_rstudio_use_the_proxy_when_the_installation_runs_it(client):
    onboard_workspace_group(client, "ana")
    from app.config import get_settings
    from app.db import SessionLocal
    from app.executions import workspace_spec
    from app.models import Workspace
    settings = get_settings().model_copy(update={"egress_proxy_url": "http://fdp-egress-proxy.fdp.svc:3128"})
    specs = {}
    for editor in ("vscode", "rstudio", "sql"):
        ws = client.post("/v1/workspaces", headers=as_("ana"), json={"name": editor, "editor": editor}).json()
        with SessionLocal() as db:
            specs[editor] = (workspace_spec(settings, db.get(Workspace, ws["id"]), [], []),
                             workspace_spec(get_settings(), db.get(Workspace, ws["id"]), [], []))
    for editor in ("vscode", "rstudio"):
        spec, without = specs[editor]
        assert spec.env["HTTPS_PROXY"] == spec.env["https_proxy"] == "http://fdp-egress-proxy.fdp.svc:3128"
        assert ".svc" in spec.env["NO_PROXY"].split(",")    # the platform itself is reached directly
        assert spec.labels["fdp.floren.app/egress"] == "proxy"
        assert "HTTPS_PROXY" not in without.env and "fdp.floren.app/egress" not in without.labels
    sql, _ = specs["sql"]
    assert "HTTPS_PROXY" not in sql.env and "fdp.floren.app/egress" not in sql.labels


def test_layers_let_marked_editors_reach_the_proxy_only_when_it_runs():
    spec = InfrastructureSpec.model_validate(managed())
    config = ControllerConfig("platform", "installation-a", "fdp-layer", "fdp-runtime", artifact_image="fdp/control-plane:1")

    def policy(cfg):
        docs = manifests(layers.NON_PROD, 1, spec, cfg, workers=WorkerPlan("trino", 1, 4, 1))
        return [d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "workspace-egress-proxy"]
    assert policy(config) == []
    [rule] = policy(ControllerConfig(**{**config.__dict__, "egress_proxy": True}))
    assert rule["spec"]["podSelector"] == {"matchLabels": {"fdp.floren.app/egress": "proxy"}}
    [egress] = rule["spec"]["egress"]
    assert egress["ports"] == [{"protocol": "TCP", "port": 3128}]
    assert egress["to"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "platform"}},
                             "podSelector": {"matchLabels": {"app.kubernetes.io/name": "fdp-egress-proxy"}}}]
