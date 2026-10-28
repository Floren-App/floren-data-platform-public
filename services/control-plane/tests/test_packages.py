"""The package repository: one repository with Python and R packages and every version, read by everyone,
published by RBAC, imported from PyPI/CRAN on request, and the only place executions install from."""
import base64
import hashlib
import io
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

from app import package_imports, packages as repo
from app.db import SessionLocal
from .conftest import as_
from .test_api import execution_token, onboard_workspace_group
from .test_workspace_templates import group

ADMIN = as_("admin")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No network in tests: nothing exists upstream unless a test says so."""
    monkeypatch.setattr(package_imports, "upstream_has", lambda kind, name: False)


def wheel(name="fdp-tools", version="1.0.0", requires=(), tag="py3-none-any", summary="Team helpers"):
    dist = f"{name.replace('-', '_')}-{version}"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: {summary}\nRequires-Python: >=3.9\n"
    meta += "".join(f"Requires-Dist: {r}\n" for r in requires)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # Fixed ZIP metadata: regenerating this fixture must have the same hash
        # even when upload and index verification cross a clock tick.
        z.writestr(zipfile.ZipInfo(f"{dist}.dist-info/METADATA"), meta)
        z.writestr(zipfile.ZipInfo(f"{name.replace('-', '_')}/__init__.py"), f"__version__ = '{version}'\n")
    return f"{dist}-{tag}.whl", buf.getvalue()


def _tar(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def r_package(name="fdpr", version="1.0.0", imports="", depends="R (>= 4.1.0)", built=False):
    desc = f"Package: {name}\nVersion: {version}\nTitle: Team R helpers\nDepends: {depends}\nLicense: MIT\n"
    if imports:
        desc += f"Imports: {imports}\n"
    if built:
        desc += "Built: R 4.4.1; ; 2026-10-01 00:00:00 UTC; unix\n"
    return f"{name}_{version}.tar.gz", _tar({f"{name}/DESCRIPTION": desc, f"{name}/R/hello.R": "hello <- function() 1\n"})


def publishers(client, *users, name="Data engineers"):
    g = group(client, name, *users)
    r = client.put("/v1/rbac/resources/packages/assignments", headers=ADMIN, json={"app_group_id": g["id"], "role": "Publisher"})
    assert r.status_code == 200, r.text
    return g


def upload(client, user, kind, filename, data):
    return client.post(f"/v1/packages/{kind}/upload", headers=as_(user), files={"file": (filename, data)})


def test_publishers_publish_packages_and_only_the_owning_group_publishes_new_versions(client):
    publishers(client, "ana")
    filename, data = wheel()
    assert upload(client, "bob", "python", filename, data).status_code == 403          # not a Publisher
    r = upload(client, "ana", "python", filename, data)
    assert r.status_code == 201, r.text
    assert r.json()["package"]["owner_group"] == "Data engineers" and r.json()["created"]
    again = upload(client, "ana", "python", filename, data)                             # the same file: no-op
    assert again.status_code == 201 and not again.json()["created"]
    changed = upload(client, "ana", "python", filename, wheel(summary="changed")[1])
    assert changed.status_code == 409 and "never change" in changed.json()["detail"]
    assert upload(client, "ana", "python", *wheel(version="1.1.0")).status_code == 201
    # Another team of Publishers may publish packages of its own, not new versions of this one.
    publishers(client, "bob", name="Marketing analysts")
    assert upload(client, "bob", "python", *wheel(version="2.0.0")).status_code == 403
    assert upload(client, "bob", "python", *wheel(name="bob-tools")).status_code == 201
    detail = client.get("/v1/packages/python/FDP_Tools", headers=as_("bob")).json()     # everyone reads
    assert [v["version"] for v in detail["versions"]] == ["1.1.0", "1.0.0"]
    assert detail["latest"] == "1.1.0" and detail["install"] == "pip install fdp-tools"
    assert detail["versions"][1]["install"] == "pip install fdp-tools==1.0.0"
    assert "publish" not in detail["actions"]
    assert "publish" in client.get("/v1/packages/python/fdp-tools", headers=as_("ana")).json()["actions"]
    audit = client.get("/v1/admin/audit?limit=50", headers=ADMIN).text
    assert "package.publish" in audit


def test_pip_installs_from_the_simple_index_with_every_version(client):
    publishers(client, "ana")
    for version in ("1.0.0", "1.1.0"):
        upload(client, "ana", "python", *wheel(version=version))
    index = client.get("/v1/packages/python/simple/", headers=as_("bob"))
    assert index.status_code == 200 and 'href="fdp-tools/"' in index.text
    page = client.get("/v1/packages/python/simple/fdp_tools/", headers=as_("bob")).text
    filename, data = wheel(version="1.0.0")
    assert f'href="../../files/fdp-tools/{filename}#sha256={hashlib.sha256(data).hexdigest()}"' in page
    assert 'data-requires-python="&gt;=3.9"' in page and "fdp_tools-1.1.0-py3-none-any.whl" in page
    listing = client.get("/v1/packages/python/simple/fdp-tools/", headers={**as_("bob"), "Accept": "application/vnd.pypi.simple.v1+json"})
    assert listing.headers["content-type"].startswith("application/vnd.pypi.simple.v1+json")
    assert listing.json()["versions"] == ["1.0.0", "1.1.0"]
    assert client.get(f"/v1/packages/python/files/fdp-tools/{filename}", headers=as_("bob")).content == data
    assert client.get("/v1/packages/python/simple/requests/", headers=as_("bob")).status_code == 404
    anonymous = client.get("/v1/packages/python/simple/")
    assert anonymous.status_code == 401 and anonymous.headers["www-authenticate"].startswith("Basic")


def test_yanked_versions_stay_installable_by_exact_pin_and_deleting_removes_them(client):
    publishers(client, "ana")
    for version in ("1.0.0", "1.1.0"):
        upload(client, "ana", "python", *wheel(version=version))
    assert client.post("/v1/packages/python/fdp-tools/versions/1.1.0/yank", headers=as_("bob"), json={"reason": "x"}).status_code == 403
    r = client.post("/v1/packages/python/fdp-tools/versions/1.1.0/yank", headers=as_("ana"), json={"reason": "broken import"})
    assert r.status_code == 200 and r.json()["latest"] == "1.0.0" and r.json()["versions"][0]["yanked"]
    assert 'data-yanked="broken import"' in client.get("/v1/packages/python/simple/fdp-tools/", headers=as_("bob")).text
    assert client.delete("/v1/packages/python/fdp-tools/versions/1.1.0/yank", headers=as_("ana")).json()["latest"] == "1.1.0"
    assert client.delete("/v1/packages/python/fdp-tools/versions/1.1.0", headers=as_("bob")).status_code == 403
    assert client.delete("/v1/packages/python/fdp-tools/versions/1.1.0", headers=as_("ana")).status_code == 204
    filename = wheel(version="1.1.0")[0]
    assert not (repo.root_dir() / "python" / "fdp-tools" / filename).exists()
    assert client.delete("/v1/packages/python/fdp-tools/versions/1.0.0", headers=as_("ana")).status_code == 204
    assert client.get("/v1/packages/python/fdp-tools", headers=as_("ana")).status_code == 404


def test_r_repository_lists_the_newest_version_and_archives_every_version(client):
    publishers(client, "ana")
    v1, v1_data = r_package(version="1.0.0")
    assert upload(client, "ana", "r", v1, v1_data).status_code == 201
    assert upload(client, "ana", "r", *r_package(version="1.1.0", imports="jsonlite (>= 1.8), utils")).status_code == 201
    packages = client.get("/v1/packages/r/src/contrib/PACKAGES", headers=as_("bob")).text
    assert "Package: fdpr\nVersion: 1.1.0\nDepends: R (>= 4.1.0)\nImports: jsonlite (>= 1.8), utils\nLicense: MIT\nMD5sum: " in packages
    import gzip
    assert gzip.decompress(client.get("/v1/packages/r/src/contrib/PACKAGES.gz", headers=as_("bob")).content).decode() == packages
    assert client.get(f"/v1/packages/r/src/contrib/Archive/fdpr/{v1}", headers=as_("bob")).content == v1_data
    assert client.get("/v1/packages/r/src/contrib/fdpr_1.1.0.tar.gz", headers=as_("bob")).status_code == 200
    assert client.get("/v1/packages/r/src/contrib/PACKAGES.rds", headers=as_("bob")).status_code == 404
    detail = client.get("/v1/packages/r/fdpr", headers=as_("bob")).json()
    assert detail["versions"][0]["requires"] == ["jsonlite (>= 1.8)", "R (>= 4.1.0)"]
    assert detail["versions"][1]["install"] == 'renv::install("fdpr@1.0.0")'
    client.post("/v1/packages/r/fdpr/versions/1.1.0/yank", headers=as_("ana"), json={})
    assert "Version: 1.0.0" in client.get("/v1/packages/r/src/contrib/PACKAGES", headers=as_("bob")).text
    # Source tarballs only, named after their DESCRIPTION; one file per version.
    assert upload(client, "ana", "r", *r_package(version="2.0.0", built=True)).status_code == 422
    assert upload(client, "ana", "r", "fdpr_3.0.0.tar.gz", r_package(version="2.0.0")[1]).status_code == 422
    assert upload(client, "ana", "r", "notes.txt", b"x").status_code == 422
    assert upload(client, "ana", "r", *r_package(name="stats")).status_code == 422


def test_published_names_never_shadow_upstream_packages(client, monkeypatch):
    publishers(client, "ana")
    monkeypatch.setattr(package_imports, "upstream_has", lambda kind, name: name == "requests")
    r = upload(client, "ana", "python", *wheel(name="requests", version="9.9.9"))
    assert r.status_code == 409 and "import it instead" in r.json()["detail"]


def fake_pip(files_by_target):
    def download(requirement, version, arch, out, home):
        out.mkdir(parents=True, exist_ok=True)
        for filename, data in files_by_target.get((version, arch), []):
            (out / filename).write_bytes(data)
        import subprocess
        ok = (version, arch) in files_by_target
        return subprocess.CompletedProcess([], 0 if ok else 1, "", "" if ok else "ERROR: No matching distribution found")
    return download


def run_imports():
    while (job_id := package_imports.claim(SessionLocal)):
        package_imports.run(SessionLocal, job_id)


def test_publishers_import_from_pypi_with_dependencies_built_packages_only(client, monkeypatch):
    publishers(client, "ana")
    pandas = [wheel("pandas", "2.2.3", requires=["numpy>=1.26"], tag="cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64"),
              wheel("numpy", "2.1.0", tag="cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64"), wheel("six", "1.17.0")]
    calls = []
    monkeypatch.setattr(package_imports, "pip_download",
                        lambda *a: calls.append(a[1:3]) or fake_pip({("3.12", "x86_64"): pandas, ("3.11", "x86_64"): [pandas[2]]})(*a))
    assert client.post("/v1/packages/imports", headers=as_("bob"), json={"kind": "python", "requirement": "pandas==2.2.3"}).status_code == 403
    for bad in ("pandas @ https://evil.test/p.whl", "not a requirement!"):
        assert client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "python", "requirement": bad}).status_code == 422
    job = client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "python", "requirement": "pandas==2.2.3"})
    assert job.status_code == 202 and job.json()["status"] == "queued"
    run_imports()
    done = client.get(f"/v1/packages/imports/{job.json()['id']}", headers=as_("ana")).json()
    assert done["status"] == "done", done["log"]
    assert sorted(done["added"]) == sorted(f for f, _ in pandas)
    assert {"3.11", "3.12"} <= {v for v, _ in calls} and {"x86_64", "aarch64"} == {a for _, a in calls}
    assert "aarch64: not available as a built package" in done["log"]
    listed = {p["name"]: p for p in client.get("/v1/packages?kind=python", headers=as_("bob")).json()["packages"]}
    assert listed["pandas"]["origin"] == "pypi" and listed["numpy"]["latest"] == "2.1.0"
    # Imported names belong to upstream: no one publishes their own "numpy".
    r = upload(client, "ana", "python", *wheel("numpy", "9.0.0"))
    assert r.status_code == 409 and "imported from PyPI" in r.json()["detail"]
    # And an upstream package never replaces one published here.
    upload(client, "ana", "python", *wheel("team-core"))
    monkeypatch.setattr(package_imports, "pip_download", fake_pip({("3.12", "x86_64"): [wheel("team-core", "5.0.0")]}))
    client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "python", "requirement": "team-core"})
    run_imports()
    [latest, *_] = client.get("/v1/packages/imports", headers=as_("ana")).json()
    assert latest["status"] == "failed" and "published here" in latest["log"]


def test_nothing_resolves_without_built_packages(client, monkeypatch):
    publishers(client, "ana")
    monkeypatch.setattr(package_imports, "pip_download", fake_pip({}))
    client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "python", "requirement": "oldlib"})
    run_imports()
    [job] = client.get("/v1/packages/imports", headers=as_("ana")).json()
    assert job["status"] == "failed" and "build a wheel and publish it" in job["log"]


def test_cran_imports_resolve_dependencies_and_take_older_versions_from_the_archive(client, monkeypatch):
    publishers(client, "ana")
    index = {"dplyr": {"Package": "dplyr", "Version": "1.1.4"}, "rlang": {"Package": "rlang", "Version": "1.1.6"},
             "MASS": {"Package": "MASS", "Version": "7.3-61", "Priority": "recommended"}}
    tarballs = {"https://cran.test/src/contrib/dplyr_1.1.4.tar.gz": r_package("dplyr", "1.1.4", imports="rlang (>= 1.1.0), MASS, methods"),
                "https://cran.test/src/contrib/rlang_1.1.6.tar.gz": r_package("rlang", "1.1.6"),
                "https://cran.test/src/contrib/Archive/dplyr/dplyr_1.0.0.tar.gz": r_package("dplyr", "1.0.0", imports="rlang")}
    fetched = []

    def download(client_, url, target):
        fetched.append(url)
        if url not in tarballs:
            return False
        target.write_bytes(tarballs[url][1])
        return True
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "package_upstream_cran", "https://cran.test")
    monkeypatch.setattr(package_imports, "cran_index", lambda max_age=3600: index)
    monkeypatch.setattr(package_imports, "_download", download)
    client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "r", "requirement": "dplyr"})
    run_imports()
    [job] = client.get("/v1/packages/imports", headers=as_("ana")).json()
    assert job["status"] == "done", job["log"]
    assert sorted(job["added"]) == ["dplyr_1.1.4.tar.gz", "rlang_1.1.6.tar.gz"]
    assert "MASS: comes with R" in job["log"]
    client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "r", "requirement": "dplyr==1.0.0"})
    run_imports()
    [older, _] = client.get("/v1/packages/imports", headers=as_("ana")).json()
    assert older["status"] == "done" and older["added"] == ["dplyr_1.0.0.tar.gz"], older["log"]
    assert "https://cran.test/src/contrib/Archive/dplyr/dplyr_1.0.0.tar.gz" in fetched
    versions = [v["version"] for v in client.get("/v1/packages/r/dplyr", headers=as_("bob")).json()["versions"]]
    assert versions == ["1.1.4", "1.0.0"]
    assert client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "r", "requirement": "stats"}).status_code == 422


def test_executions_install_only_from_the_repository_and_publish_with_their_token(client):
    onboard_workspace_group(client, "ana")
    ws = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "w", "editor": "vscode"}).json()
    client.post(f"/v1/workspaces/{ws['id']}/start", headers=as_("ana"))
    spec, headers = execution_token(ws["id"])
    token = spec.env["FDP_SESSION_TOKEN"]
    index, cran = spec.env["PIP_INDEX_URL"], spec.env["FDP_CRAN"]
    # Installers read with a short package key in the address, never the execution's token (they print addresses).
    assert token not in index and token not in cran and "@" not in index
    key = index.split("/x/")[1].split("/")[0]
    assert index == f"http://control-plane:8000/v1/packages/x/{key}/python/simple/" and len(key) < 40
    assert cran == spec.env["RENV_CONFIG_REPOS_OVERRIDE"] == f"http://control-plane:8000/v1/packages/x/{key}/r"
    assert spec.env["PIP_TRUSTED_HOST"] == "control-plane:8000"
    assert spec.env["TWINE_REPOSITORY_URL"].endswith("/v1/packages/python/legacy/") and spec.env["TWINE_PASSWORD"] == token
    assert client.get(f"/v1/packages/x/{key}/python/simple/").status_code == 200
    assert client.get(f"/v1/packages/x/{key}/r/src/contrib/PACKAGES").status_code == 200
    forged = key[:-2] + ("AA" if not key.endswith("AA") else "BB")
    assert client.get(f"/v1/packages/x/{forged}/python/simple/").status_code == 401
    assert client.post(f"/v1/packages/x/{key}/python/upload", files={"file": ("a.whl", b"x")}).status_code in (404, 405)
    basic = {"Authorization": "Basic " + base64.b64encode(f"fdp:{token}".encode()).decode()}
    assert client.get("/v1/packages/python/simple/", headers=basic).status_code == 200
    # twine from the workspace publishes as the person, with their rights.
    filename, data = wheel()
    twine = {":action": "file_upload", "protocol_version": "1", "name": "fdp-tools", "version": "1.0.0"}
    tw_headers = {"Authorization": "Basic " + base64.b64encode(f"__token__:{token}".encode()).decode()}
    assert client.post("/v1/packages/python/legacy/", headers=tw_headers, data=twine, files={"content": (filename, data)}).status_code == 403
    publishers(client, "ana")
    r = client.post("/v1/packages/python/legacy/", headers=tw_headers, data=twine, files={"content": (filename, data)})
    assert r.status_code == 200 and r.text == "OK"
    # A workspace token keeps working past its expiry while the workspace runs; not after it stops.
    from app.auth import mint_token
    expired = mint_token("execution", "ana", -60, ws=ws["id"], pid=ws.get("principal_id", ""))
    old = {"Authorization": "Basic " + base64.b64encode(f"fdp:{expired}".encode()).decode()}
    assert client.get("/v1/packages/python/simple/", headers=old).status_code == 200
    client.post(f"/v1/workspaces/{ws['id']}/stop", headers=as_("ana"))
    assert client.get("/v1/packages/python/simple/", headers=old).status_code == 401
    assert client.get(f"/v1/packages/x/{key}/python/simple/").status_code == 401   # the key ends with the workspace


def test_keyed_index_serves_files_through_relative_links(client):
    publishers(client, "ana")
    filename, data = wheel()
    upload(client, "ana", "python", filename, data)
    onboard_workspace_group(client, "bob")
    ws = client.post("/v1/workspaces", headers=as_("bob"), json={"name": "w", "editor": "vscode"}).json()
    client.post(f"/v1/workspaces/{ws['id']}/start", headers=as_("bob"))
    index = execution_token(ws["id"])[0].env["PIP_INDEX_URL"].replace("http://control-plane:8000", "")
    page = client.get(index + "fdp-tools/").text
    href = page.split('href="')[1].split("#")[0]
    from urllib.parse import urljoin
    assert client.get(urljoin(index + "fdp-tools/", href)).content == data


def test_public_package_indexes_can_not_be_allowlisted(client):
    for host in ("pypi.org", "files.pythonhosted.org", "*.pythonhosted.org", "cloud.r-project.org", "*.r-project.org"):
        r = client.put("/v1/admin/settings/egress", headers=ADMIN, json={"hosts": [{"host": host}]})
        assert r.status_code == 422 and "package repository" in r.json()["detail"], host
    from app.egress_proxy import Proxy
    proxy = Proxy("http://unused")
    proxy.patterns = ["*"]
    assert "package repository" in proxy.check("files.pythonhosted.org", "443")


def test_runs_install_with_a_key_of_their_own(client):
    from app import deps
    onboard_workspace_group(client, "ana")
    ws = client.post("/v1/workspaces", headers=as_("ana"), json={"name": "w", "editor": "vscode"}).json()
    run = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws["id"], "kind": "script", "entrypoint": "x.py", "pool": "small"})
    assert run.status_code == 201, run.text
    env = deps.get_orchestrator().executions["run-" + run.json()["id"]].env
    key = env["PIP_INDEX_URL"].split("/x/")[1].split("/")[0]
    assert key.startswith("r" + run.json()["id"] + ".") and env["TWINE_PASSWORD"] == env["FDP_SESSION_TOKEN"]
    assert client.get(f"/v1/packages/x/{key}/python/simple/").status_code == 200


def test_the_package_allowlist_limits_which_public_packages_can_be_imported(client, monkeypatch):
    publishers(client, "ana")
    monkeypatch.setattr(package_imports, "pip_download", fake_pip({("3.12", "x86_64"): [wheel("pandas", "2.2.3", requires=["numpy"]),
                                                                                         wheel("numpy", "2.1.0")]}))
    assert client.get("/v1/packages/allowlist", headers=as_("bob")).json() == {"enabled": False, "python": [], "r": []}
    body = {"python": [{"name": "Pandas", "description": "Data frames"}, {"name": "scikit-*"}], "r": [{"name": "dplyr"}, {"name": "DBI"}]}
    assert client.put("/v1/packages/allowlist", headers=as_("ana"), json=body).status_code == 403    # administrators only
    r = client.put("/v1/packages/allowlist", headers=ADMIN, json=body)
    assert r.status_code == 200 and r.json()["python"] == [{"name": "pandas", "description": "Data frames"}, {"name": "scikit-*", "description": ""}]
    assert [e["name"] for e in r.json()["r"]] == ["dplyr", "DBI"]                 # R names keep their case
    for bad in ({"python": [{"name": "../x"}]}, {"r": [{"name": "dplyr"}, {"name": "DPLYR"}]}, {"python": [{"name": "a*b"}]}):
        assert client.put("/v1/packages/allowlist", headers=ADMIN, json=bad).status_code == 422, bad
    refused = client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "python", "requirement": "requests"})
    assert refused.status_code == 403 and "allowlist" in refused.json()["detail"]
    for allowed in ("pandas==2.2.3", "scikit-learn>=1.5", "PANDAS"):
        assert client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "python", "requirement": allowed}).status_code == 202, allowed
    assert client.post("/v1/packages/imports", headers=ADMIN, json={"kind": "r", "requirement": "glue"}).status_code == 403   # admins too
    # Dependencies of an allowed package come along.
    run_imports()
    names = {p["name"] for p in client.get("/v1/packages", headers=as_("bob")).json()["packages"]}
    assert {"pandas", "numpy"} <= names
    # Published packages are not affected; turning it off allows everything again.
    assert upload(client, "ana", "python", *wheel("team-tools")).status_code == 201
    assert client.delete("/v1/packages/allowlist", headers=ADMIN).status_code == 204
    assert client.post("/v1/packages/imports", headers=as_("ana"), json={"kind": "python", "requirement": "requests"}).status_code == 202
