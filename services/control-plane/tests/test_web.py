"""The app in the browser: the gateway routes the public address to it, and Keycloak learns its address."""
import json

import httpx

from app import keycloak_clients
from app.config import get_settings

GATEWAY = {"X-FDP-Gateway-Config": "test-gateway-config-token"}


def test_the_gateway_serves_the_app_behind_every_other_route(client, monkeypatch):
    assert "web" not in client.get("/internal/traefik/config", headers=GATEWAY).json()["http"]["routers"]
    monkeypatch.setattr(get_settings(), "web_url", "http://fdp-web.fdp.svc:8080")
    http = client.get("/internal/traefik/config", headers=GATEWAY).json()["http"]
    web = http["routers"]["web"]
    assert web["priority"] == 1 and web["entryPoints"] == ["websecure"] and web["rule"].startswith("Host(`")
    assert http["services"]["web"] == {"loadBalancer": {"servers": [{"url": "http://fdp-web.fdp.svc:8080"}]}}
    assert all(r.get("priority", 2) > 1 for name, r in http["routers"].items() if name != "web")


def keycloak(clients, calls):
    def handle(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t"})
        if request.method == "GET":
            return httpx.Response(200, json=clients)
        clients[0].update(json.loads(request.content))
        return httpx.Response(204)
    return lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw)


def test_keycloak_gets_the_app_addresses_from_the_values_and_nothing_else():
    clients = [{"id": "c1", "clientId": "fdp-desktop", "redirectUris": ["tauri://localhost/*"], "webOrigins": ["+"],
                "attributes": {"pkce.code.challenge.method": "S256"}, "publicClient": True}]
    calls = []
    uris = ["tauri://localhost/*", "https://fdp.example.com/*"]
    assert keycloak_clients.sync("http://kc", "fdp", "fdp-desktop", "admin", "pw", uris, http=keycloak(clients, calls))
    assert clients[0]["redirectUris"] == uris and clients[0]["publicClient"] is True
    assert clients[0]["attributes"] == {"pkce.code.challenge.method": "S256", "post.logout.redirect.uris": "+"}
    assert ("PUT", "/admin/realms/fdp/clients/c1") in calls
    calls.clear()   # already right: nothing written
    assert not keycloak_clients.sync("http://kc", "fdp", "fdp-desktop", "admin", "pw", uris, http=keycloak(clients, calls))
    assert all(method != "PUT" for method, _ in calls)
