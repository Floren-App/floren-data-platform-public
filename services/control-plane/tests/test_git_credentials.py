"""People's own Git tokens: kept in the secret store and served only to git in their own running editor."""
from app import deps, git_credentials, layers
from app.auth import mint_token
from .conftest import as_
from .test_api import execution_token, onboard_workspace_group
from .test_workspace_templates import group, template

TOKEN = "ghp_personal-token-value"


def workspace(client, user, editor="vscode"):
    onboard_workspace_group(client, user)
    ws = client.post("/v1/workspaces", headers=as_(user), json={"name": f"{user}-{editor}", "editor": editor}).json()
    r = client.post(f"/v1/workspaces/{ws['id']}/start", headers=as_(user))
    assert r.json()["status"] == "running", r.text
    return ws


def credential(client, ws_id, headers, host="github.com", protocol="https"):
    return client.get(f"/v1/workspaces/{ws_id}/git-credential", params={"host": host, "protocol": protocol}, headers=headers)


def save(client, user="ana", **fields):
    body = {"host": "github.com", "username": f"{user}-gh", "token": TOKEN, **fields}
    r = client.post("/v1/me/git-credentials", headers=as_(user), json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_people_keep_several_credentials_whose_tokens_are_never_shown_again(client):
    work = save(client, label="GitHub work", host="GitHub.com", commit_name="Ana Analyst", commit_email="ana@floren.app")
    assert TOKEN not in str(work) and work["host"] == "github.com" and work["label"] == "GitHub work"
    personal = save(client, token="ghp_other", commit_email="12+ana@users.noreply.github.com")
    assert personal["label"] == "github.com"                                   # the host when no label is given
    assert [c["label"] for c in client.get("/v1/me/git-credentials", headers=as_("ana")).json()] == ["GitHub work", "github.com"]
    assert client.get("/v1/me/git-credentials", headers=as_("bob")).json() == []      # your own only
    assert deps.get_secret_store().read(git_credentials.path("ana", work["id"])) == {"token": TOKEN}
    audit = client.get("/v1/admin/audit?limit=20", headers=as_("admin")).text
    assert "git.credential.create" in audit and TOKEN not in audit
    for bad in ({"host": "github.com/org"}, {"host": "-x.com"}, {"commit_email": "not-an-email"}, {"token": ""}):
        assert client.post("/v1/me/git-credentials", headers=as_("ana"), json={"host": "github.com", "username": "a", "token": "t", **bad}).status_code == 422, bad
    assert client.post("/v1/me/git-credentials", headers=as_("ana"), json={"label": "github work", "host": "github.com", "username": "a", "token": "t"}).status_code == 409
    # Updating without a token keeps the saved one.
    r = client.put(f"/v1/me/git-credentials/{work['id']}", headers=as_("ana"), json={"label": "GitHub work", "host": "github.com", "username": "ana-work"})
    assert r.status_code == 200 and r.json()["username"] == "ana-work"
    assert deps.get_secret_store().read(git_credentials.path("ana", work["id"])) == {"token": TOKEN}
    assert client.put(f"/v1/me/git-credentials/{work['id']}", headers=as_("bob"), json={"host": "github.com", "username": "x"}).status_code == 404
    assert client.delete(f"/v1/me/git-credentials/{work['id']}", headers=as_("bob")).status_code == 404
    assert client.delete(f"/v1/me/git-credentials/{work['id']}", headers=as_("ana")).status_code == 204
    assert deps.get_secret_store().read(git_credentials.path("ana", work["id"])) == {}


def test_each_workspace_uses_the_credential_its_owner_chose(client):
    work = save(client, label="GitHub work", commit_name="Ana Analyst", commit_email="ana@floren.app")
    save(client, label="GitHub personal", token="ghp_personal")
    onboard_workspace_group(client, "ana")
    ws = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "w", "editor": "vscode"}).json()
    assert ws["git_asked"] is False and ws["git_credential_id"] is None          # the app asks at the first start
    r = client.put(f"/v1/workspaces/{ws['id']}/git", headers=as_("ana"), json={"credential_id": work["id"]})
    assert r.status_code == 200 and r.json()["git_asked"] and r.json()["git_credential_id"] == work["id"]
    client.post(f"/v1/workspaces/{ws['id']}/start", headers=as_("ana"))
    spec, headers = execution_token(ws["id"])
    assert (spec.env["FDP_GIT_NAME"], spec.env["FDP_GIT_EMAIL"]) == ("Ana Analyst", "ana@floren.app")
    assert credential(client, ws["id"], headers).text == f"username=ana-gh\npassword={TOKEN}\n"
    # Switching applies to the token right away.
    personal = next(c for c in client.get("/v1/me/git-credentials", headers=as_("ana")).json() if c["label"] == "GitHub personal")
    client.put(f"/v1/workspaces/{ws['id']}/git", headers=as_("ana"), json={"credential_id": personal["id"]})
    assert "ghp_personal" in credential(client, ws["id"], headers).text
    # No Git: nothing, with the reason.
    client.put(f"/v1/workspaces/{ws['id']}/git", headers=as_("ana"), json={"credential_id": None})
    none = credential(client, ws["id"], headers)
    assert none.status_code == 404 and "uses no Git credential" in none.json()["detail"]
    # Only your own credentials and editors with Git; deleting the chosen credential asks again.
    bob = save(client, "bob")
    assert client.put(f"/v1/workspaces/{ws['id']}/git", headers=as_("ana"), json={"credential_id": bob["id"]}).status_code == 404
    assert client.put(f"/v1/workspaces/{ws['id']}/git", headers=as_("bob"), json={"credential_id": bob["id"]}).status_code == 404
    sql = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "q", "editor": "sql"}).json()
    assert client.put(f"/v1/workspaces/{sql['id']}/git", headers=as_("ana"), json={"credential_id": work["id"]}).status_code == 422
    client.put(f"/v1/workspaces/{ws['id']}/git", headers=as_("ana"), json={"credential_id": work["id"]})
    client.delete(f"/v1/me/git-credentials/{work['id']}", headers=as_("ana"))
    after = next(w for w in client.get("/v1/workspaces", headers=as_("ana")).json() if w["id"] == ws["id"])
    assert after["git_asked"] is False and after["git_credential_id"] is None


def test_only_git_in_your_own_running_editor_gets_your_token(client):
    save(client)
    ws = workspace(client, "ana")
    _, headers = execution_token(ws["id"])
    r = credential(client, ws["id"], headers)      # no choice made yet: the only credential for the host
    assert r.status_code == 200 and r.text == f"username=ana-gh\npassword={TOKEN}\n"
    assert r.headers["cache-control"] == "no-store"
    missing = credential(client, ws["id"], headers, host="gitlab.com")
    assert missing.status_code == 404 and "Account → Git credentials" in missing.json()["detail"]
    assert credential(client, ws["id"], headers, protocol="http").status_code == 404
    save(client, label="second")                   # two for the host and none chosen: ask to choose
    assert "several credentials" in credential(client, ws["id"], headers).json()["detail"]
    # Another person's workspace, a person's own login, or a run started in the workspace get nothing.
    bob = workspace(client, "bob")
    _, bob_headers = execution_token(bob["id"])
    assert credential(client, ws["id"], bob_headers).status_code == 403
    assert credential(client, bob["id"], bob_headers).status_code == 404       # bob saved no token
    assert credential(client, ws["id"], as_("ana")).status_code == 401
    run = mint_token("execution", "ana", 60, ws=ws["id"], run="r1")
    assert credential(client, ws["id"], {"Authorization": f"Bearer {run}"}).status_code == 401
    # The SQL editor has no Git; a stopped workspace's token no longer works.
    sql = workspace(client, "ana", "sql")
    _, sql_headers = execution_token(sql["id"])
    assert credential(client, sql["id"], sql_headers).status_code == 403
    client.post(f"/v1/workspaces/{ws['id']}/stop", headers=as_("ana"))
    assert credential(client, ws["id"], headers).status_code == 401


def test_tokens_saved_per_host_before_labels_still_work(client):
    from app.db import SessionLocal
    from app.models import GitCredential
    with SessionLocal() as db:
        row = GitCredential(owner="ana", label="github.com", host="github.com", username="ana-gh")
        db.add(row)
        db.commit()
    deps.get_secret_store().put(git_credentials.legacy_path("ana", "github.com"), {"token": TOKEN})
    ws = workspace(client, "ana")
    assert TOKEN in credential(client, ws["id"], execution_token(ws["id"])[1]).text


def test_template_repositories_are_cloned_at_first_start(client):
    r = client.put("/v1/admin/repos/analytics", headers=as_("admin"),
                   json={"name": "analytics", "url": "https://git.test/org/analytics.git", "default_branch": "develop"})
    assert r.status_code == 200, r.text
    g = group(client, "Analysts", "ana")
    template(client, g, repositories=[r.json()["id"]])
    onboard_workspace_group(client, "ana")
    [ws] = [w for w in client.get("/v1/workspaces", headers=as_("ana")).json() if w.get("template_id")]
    assert client.post(f"/v1/workspaces/{ws['id']}/start", headers=as_("ana")).json()["status"] == "running"
    spec, _ = execution_token(ws["id"])
    assert spec.env["FDP_GIT_CLONE"] == "analytics\tdevelop\thttps://git.test/org/analytics.git"
    assert ws["layer_id"] == layers.NON_PROD
    own = workspace(client, "ana")                     # workspaces without a template clone nothing
    assert "FDP_GIT_CLONE" not in execution_token(own["id"])[0].env
