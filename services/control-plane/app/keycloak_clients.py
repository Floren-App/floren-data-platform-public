"""Keeps the bundled Keycloak's app client in line with the Helm values after every install and upgrade.

Keycloak imports the realm only when it does not exist yet, so later Helm changes (the browser app's address,
the desktop app's redirect addresses) would never reach it. This sets the client's redirect addresses and web
origins from the values; nothing else in the realm is touched. Run with ``python -m app.keycloak_clients``
(the chart's post-install/post-upgrade hook), using the bootstrap administrator.
"""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import quote

import httpx


def desired(client: dict, redirect_uris: list) -> dict:
    attributes = {**(client.get("attributes") or {}), "post.logout.redirect.uris": "+"}
    return {**client, "redirectUris": redirect_uris, "webOrigins": ["+"], "attributes": attributes}


def sync(base: str, realm: str, client_id: str, username: str, password: str, redirect_uris: list,
         http=httpx.Client, deadline: float = 600) -> bool:
    """True when the client changed. Waits for Keycloak to come up (it may restart with the same upgrade)."""
    started = time.monotonic()
    while True:
        try:
            with http(base_url=base, timeout=15) as c:
                token = c.post("/realms/master/protocol/openid-connect/token",
                               data={"grant_type": "password", "client_id": "admin-cli", "username": username, "password": password})
                token.raise_for_status()
                c.headers["Authorization"] = "Bearer " + token.json()["access_token"]
                path = f"/admin/realms/{quote(realm, safe='')}/clients"
                found = c.get(path, params={"clientId": client_id})
                found.raise_for_status()
                if not found.json():
                    raise SystemExit(f"client {client_id} not found in realm {realm}")
                current = found.json()[0]
                wanted = desired(current, redirect_uris)
                if (sorted(current.get("redirectUris") or []) == sorted(redirect_uris) and current.get("webOrigins") == ["+"]
                        and (current.get("attributes") or {}).get("post.logout.redirect.uris") == "+"):
                    return False
                c.put(f"{path}/{current['id']}", json=wanted).raise_for_status()
                return True
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            if time.monotonic() - started > deadline:
                raise SystemExit(f"Keycloak could not be updated: {type(exc).__name__}") from None
            time.sleep(5)


def main() -> None:
    env = os.environ
    changed = sync(env["KEYCLOAK_URL"], env["KEYCLOAK_REALM"], env["KEYCLOAK_CLIENT_ID"], env["KEYCLOAK_ADMIN_USER"],
                   env["KEYCLOAK_ADMIN_PASSWORD"], json.loads(env["KEYCLOAK_REDIRECT_URIS"]))
    print(f"{env['KEYCLOAK_CLIENT_ID']}: {'updated' if changed else 'already up to date'}", file=sys.stderr)


if __name__ == "__main__":
    main()
