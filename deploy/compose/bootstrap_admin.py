"""Provision the local Compose admin through SCIM, including on an empty database."""
import json
import os
import time
import urllib.error
import urllib.request

base = os.environ.get("FDP_BOOTSTRAP_API", "http://control-plane:8000").rstrip("/")
token = os.environ["FDP_SCIM_TOKEN"]


def request(method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/scim+json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


for attempt in range(60):
    try:
        users = request("GET", "/scim/v2/Users?filter=userName%20eq%20%22admin%22")["Resources"]
        break
    except (urllib.error.URLError, TimeoutError):
        if attempt == 59:
            raise
        time.sleep(1)

admin = next((u for u in users if u["userName"] == "admin"), None)
roles = (admin or {}).get("roles", [])
if not admin or not admin.get("active") or not any(r.get("value") == "fdp-admin" for r in roles):
    if not any(r.get("value") == "fdp-admin" for r in roles):
        roles.append({"value": "fdp-admin"})
    body = {"userName": "admin", "externalId": (admin or {}).get("externalId") or "local-admin",
            "active": True, "roles": roles}
    request("PUT" if admin else "POST", "/scim/v2/Users" + ("/" + admin["id"] if admin else ""), body)
print("Local platform administrator is provisioned (admin/admin).")
