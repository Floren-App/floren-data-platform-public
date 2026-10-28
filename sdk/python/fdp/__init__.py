"""Floren Data Platform SDK.

    import fdp
    engine = fdp.connect("finance-dw")          # SQLAlchemy engine
    df = pandas.read_sql("select * from sales", engine)

    fdp.run("models/train.py", pool="large")   # run on a bigger compute pool

Credentials are fetched at call time with the workspace's session token; they are
never written to disk and never visible in the editor.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, NamedTuple, Optional

import httpx

__all__ = ["api", "certificate", "args", "output_dir", "connect", "credentials", "sources", "run", "run_program", "runs", "programs",
           "query", "orchestrate", "run_logs", "run_status", "stop_run", "files",
           "measure_metric", "report_metric", "observe_dataset"]


def args() -> Dict[str, Any]:
    """Typed argument values for this run (from fdp.json + the run form). Empty outside a run."""
    import json

    return json.loads(os.environ.get("FDP_ARGS", "{}"))


def output_dir() -> str:
    """Where to write files that should show up as run outputs (cwd outside a run)."""
    path = os.environ.get("FDP_OUTPUT_DIR", ".")
    os.makedirs(path, exist_ok=True)
    return path


RESPONSE_LIMIT = 10 * 1024 * 1024  # what an internal API returns at most


def respond(data: Any = None, status: int = 200) -> str:
    """Answer the internal API call that started this run: `data` becomes the caller's result and `status`
    the HTTP status code (e.g. respond({"error": "unknown region"}, status=404) to answer with an error).

    Anything JSON-serialisable works; a pandas DataFrame becomes a list of rows, and dates or decimals
    become text. Returns the path of the response.json written to this run's outputs."""
    import json

    if not isinstance(status, int) or not 200 <= status <= 599 or status in (204, 304):
        raise ValueError("status must be an HTTP status code from 200 to 599 that carries a body (not 204 or 304)")
    if hasattr(data, "to_dict") and hasattr(data, "columns"):
        data = data.to_dict(orient="records")
    body = json.dumps(data, default=str).encode()
    if len(body) > RESPONSE_LIMIT:
        raise ValueError(f"the answer is {len(body)} bytes; internal APIs return at most {RESPONSE_LIMIT} bytes")
    folder = output_dir()
    for name, content in (("response.json", body), ("response.status", str(status).encode())):
        path = os.path.join(folder, name)
        with open(path + ".tmp", "wb") as f:  # never serve a half-written answer
            f.write(content)
        os.replace(path + ".tmp", path)
    return os.path.join(folder, "response.json")


def _auth_token(token: Optional[str] = None) -> str:
    """Resolve/renew authentication without constructing an HTTP client per SQL page."""
    api = os.environ.get("FDP_API")
    token = token or os.environ.get("FDP_TOKEN") or os.environ.get("FDP_SESSION_TOKEN")
    if not api or not token:
        raise RuntimeError("set FDP_API and pass token= (a service token) outside Floren Data Platform")
    if token == os.environ.get("FDP_SESSION_TOKEN"):
        import base64, json, time
        try:
            part = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            if claims.get("run") and claims.get("exp", 0) < time.time() + 60:
                renewed = httpx.post(f"{api}/v1/executions/token", headers={"Authorization": f"Bearer {token}"}, timeout=30)
                renewed.raise_for_status()
                token = renewed.json()["token"]
                os.environ["FDP_SESSION_TOKEN"] = token
        except (ValueError, IndexError):
            pass
    return token


def _client(token: Optional[str] = None) -> httpx.Client:
    """Workspace identity, or an explicit service token for external clients."""
    return httpx.Client(base_url=os.environ.get("FDP_API"),
                        headers={"Authorization": "Bearer " + _auth_token(token)}, timeout=30)


def _get(path: str, token: Optional[str] = None) -> Any:
    with _client(token) as c:
        r = c.get(path)
        r.raise_for_status()
        return r.json()


def sources() -> List[Dict[str, Any]]:
    """Data sources your groups have been granted."""
    return _get("/v1/connect")


def credentials(name: str, token: Optional[str] = None, schema: Optional[str] = None,
                viewer_token: Optional[str] = None) -> Dict[str, Any]:
    """Raw connection info: {'kind', 'config', 'credentials'}. Prefer connect()."""
    from urllib.parse import quote
    with _client(token) as c:
        r = c.get(f"/v1/connect/{quote(name, safe='')}", params={"schema": schema} if schema else {},
                  headers={"X-FDP-Viewer": viewer_token} if viewer_token else {})
        r.raise_for_status()
        return r.json()


_SQL_DRIVERS = {"postgres": "postgresql+psycopg", "mysql": "mysql+pymysql", "mssql": "mssql+pyodbc"}


def connect(name: str, token: Optional[str] = None, schema: Optional[str] = None,
            viewer_token: Optional[str] = None, **kwargs: Any) -> Any:
    """Returns a ready client: SQLAlchemy engine for SQL sources, boto3 client for S3.

    token: a service token, to connect as that service account (e.g. from outside Floren Data Platform)."""
    info = credentials(name, token, schema=schema, viewer_token=viewer_token)
    kind, cfg, cred = info["kind"], info["config"], info["credentials"]
    if kind == "trino":
        from .sql import connection
        return connection(info, token, viewer_token, **kwargs)
    if kind in _SQL_DRIVERS:
        from sqlalchemy import URL, create_engine

        url = URL.create(
            _SQL_DRIVERS[kind], username=cred.get("username"), password=cred.get("password"),
            host=cfg.get("host"), port=cfg.get("port"), database=cfg.get("database"),
        )
        return create_engine(url, **kwargs)
    if kind == "s3":
        return files(name, token=token).client         # boto3 client; files() is usually handier
    if kind == "iceberg":
        from .lake import iceberg_catalog

        return iceberg_catalog(info)                   # PyIceberg catalog
    raise NotImplementedError(f"no built-in client for {kind!r}; use fdp.credentials({name!r})")


def run(entrypoint: str, pool: str = "", kind: str = "script") -> Dict[str, Any]:
    """Run a script/notebook/app from this workspace on a warehouse (default: the platform's default warehouse)."""
    body: Dict[str, Any] = {"workspace_id": os.environ["FDP_WORKSPACE_ID"], "kind": kind, "entrypoint": entrypoint}
    if pool:
        body["pool"] = pool
    with _client() as c:
        r = c.post("/v1/runs", json=body)
        r.raise_for_status()
        return r.json()


def run_program(program: str, warehouse: str = "", **arguments: Any) -> Dict[str, Any]:
    """Run a program defined in fdp.json, e.g. run_program("sales-report", start_date="2026-01-01")."""
    body: Dict[str, Any] = {"workspace_id": os.environ["FDP_WORKSPACE_ID"], "program": program, "args": arguments}
    if warehouse:
        body["pool"] = warehouse
    with _client() as c:
        r = c.post("/v1/runs", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"cannot start {program}: {r.json().get('detail')}")
        return r.json()


def programs() -> Dict[str, Any]:
    """This workspace's fdp.json as the control plane sees it (validated)."""
    return _get(f"/v1/workspaces/{os.environ['FDP_WORKSPACE_ID']}/config")


def runs() -> List[Dict[str, Any]]:
    return _get("/v1/runs")


def orchestrate(program: Optional[str] = None, workload: Optional[str] = None,
                args: Optional[Dict[str, Any]] = None, warehouse: Optional[str] = None) -> Dict[str, Any]:
    """Start a run from an orchestrator (used by fdp.airflow.FDPRunOperator)."""
    body: Dict[str, Any] = {"args": args or {}}
    if program:
        body["program"] = program
    if workload:
        body["workload"] = workload
    if warehouse:
        body["warehouse"] = warehouse
    with _client() as c:
        r = c.post("/v1/orchestrate/runs", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"cannot start {program or workload}: {r.json().get('detail')}")
        return r.json()


def run_logs(run_id: str) -> str:
    with _client() as c:
        return c.get(f"/v1/runs/{run_id}/logs", params={"tail": 20000}).text


def run_status(run_id: str) -> str:
    return _get(f"/v1/runs/{run_id}")["status"]


def stop_run(run_id: str) -> Dict[str, Any]:
    with _client() as c:
        return c.post(f"/v1/runs/{run_id}/stop").json()


def files(name: str, token: Optional[str] = None, *, viewer_token: Optional[str] = None) -> Any:
    """Files in an S3-compatible data source (RustFS, MinIO, S3), within its bucket/prefix:
    files("lake-files").read_json("x.json"), .write_text(...), .list("reports/")."""
    from .lake import Files, DelegatedFiles
    if viewer_token:
        return DelegatedFiles(name, viewer_token)

    info = credentials(name, token)
    if info["kind"] not in ("s3", "iceberg"):
        raise ValueError(f"{name} is a {info['kind']} data source, not object storage")
    if info["kind"] == "iceberg":
        cfg = info["config"]
        bucket, _, prefix = (cfg.get("storage") or "").removeprefix("s3://").partition("/")
        info = {**info, "config": {"endpoint_url": cfg.get("s3_endpoint"), "region": cfg.get("region"),
                                   "bucket": bucket, "prefix": prefix}}
    return Files(info)


def query(source: str, sql: str, parameters=None, *, viewer_token: Optional[str] = None,
          max_rows: int = 1000, token: Optional[str] = None) -> Dict[str, Any]:
    """Shared caller-mode apps: pass the gateway's X-FDP-Viewer header per request.

    Keep this token in viewer-local state. Do not cache query results across viewer contexts.
    The server evaluates the viewer's current permissions and keeps credentials server-side.
    """
    with _client(token) as c:
        r = c.post("/v1/delegated/query" if viewer_token else "/v1/sql/query",
                   headers={"X-FDP-Viewer": viewer_token} if viewer_token else {},
                   json={"source": source, "sql": sql, "parameters": parameters or [], "max_rows": max_rows}, timeout=45)
        r.raise_for_status()
        return r.json()


class Certificate(NamedTuple):
    """File paths for a certificate secret; key and ca are None when the secret has none."""
    cert: str
    key: Optional[str]
    ca: Optional[str]


def certificate(var: str) -> Certificate:
    """Write a certificate secret (fdp.json "secrets": {"VAR": "me/name"}) to private files for TLS clients.

    The program receives the certificate as $VAR, the private key as $VAR_KEY and the CA chain as $VAR_CA.
    Most clients want files: requests.get(url, cert=(c.cert, c.key), verify=c.ca or True).
    """
    import tempfile
    pem = os.environ.get(var)
    if not pem or "BEGIN CERTIFICATE" not in pem:
        raise KeyError(f"{var} is not a certificate secret of this run; add it under 'secrets' in fdp.json")
    folder = tempfile.mkdtemp(prefix=f"fdp-{var.lower()}-")   # mode 0700
    def write(name: str, content: Optional[str]) -> Optional[str]:
        if not content:
            return None
        path = os.path.join(folder, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path
    return Certificate(write("cert.pem", pem), write("key.pem", os.environ.get(var + "_KEY")),
                       write("ca.pem", os.environ.get(var + "_CA")))


class Api:
    """An external API from API management, called as this program's identity.

    Floren Data Platform adds the API's headers, credentials and client certificate; the program never holds them, and
    only declared operations can be called (with the Consumer role on them).
    """

    def __init__(self, name: str, token: Optional[str] = None):
        self.name, self._token = name, token

    def request(self, method: str, path: str = "/", **kwargs: Any) -> httpx.Response:
        api = os.environ.get("FDP_API")
        headers = {**kwargs.pop("headers", {}), "Authorization": "Bearer " + _auth_token(self._token)}
        url = f"{api}/v1/egress/{self.name}/{path.lstrip('/')}"
        with httpx.Client(timeout=kwargs.pop("timeout", 130)) as client:
            return client.request(method, url, headers=headers, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)

    def soap(self, operation: str, body: str, **kwargs: Any) -> httpx.Response:
        """POST a SOAP body element (or a full envelope) to a SOAP operation by name."""
        headers = {**kwargs.pop("headers", {}), "Content-Type": "text/xml"}
        return self.request("POST", operation, content=body.encode(), headers=headers, **kwargs)


def api(name: str, token: Optional[str] = None) -> Api:
    """fdp.api("crm").get("/customers/7").json()"""
    return Api(name, token)


from .quality import measure_metric, report_metric, observe_dataset
