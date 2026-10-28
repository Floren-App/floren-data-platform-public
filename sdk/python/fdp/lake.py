"""Object storage (S3-compatible: RustFS, MinIO, AWS S3) and Iceberg data sources.

Data source kinds:
  s3       config: endpoint_url, bucket, prefix, region    secret: access_key_id, secret_access_key
  iceberg  config: catalog_uri, warehouse, s3_endpoint, region
           secret: access_key_id, secret_access_key[, catalog_token]

    files = fdp.files("lake-files")          # within the granted bucket/prefix
    files.write_json("reports/today.json", {"ok": True})
    files.read_text("notes/readme.txt")
    files.list("reports/")

    engine = fdp.connect("lake")             # governed SQL through Trino
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, Iterable, List, Optional


class _FileLineage:
    """Which catalog files a deployed job reads and writes, reported to the catalog automatically.

    Nothing to declare in fdp.json: file paths are matched to the granted namespaces' prefixes and sent
    at exit (and in batches). Reporting is best-effort and never affects the job; it only names objects the
    job could access anyway, and never grants anything.
    """

    def __init__(self) -> None:
        self.pending: Dict[str, Dict[tuple, None]] = {"inputs": {}, "outputs": {}}
        self.registered = False

    def note(self, source: str, namespaces: Dict[str, str], path: str, field: str) -> None:
        import os
        if not os.environ.get("FDP_WORKLOAD_ID") or not namespaces:
            return  # workspaces and external tools: nothing to attribute
        best = None
        for name, prefix in namespaces.items():
            prefix = prefix.strip("/")
            if (not prefix or path == prefix or path.startswith(prefix + "/")) and (best is None or len(prefix) > len(best[1])):
                best = (name, prefix)
        if best is None:
            return
        name = path[len(best[1]):].lstrip("/") if best[1] else path
        if not name:
            return
        self.pending[field][(source, best[0], name)] = None
        if not self.registered:
            import atexit
            atexit.register(self.flush)
            self.registered = True
        if sum(len(v) for v in self.pending.values()) >= 200:
            self.flush()

    def flush(self) -> None:
        if not any(self.pending.values()):
            return
        refs = {f: [{"source": s, "schema": n, "name": p} for s, n, p in items] for f, items in self.pending.items()}
        self.pending = {"inputs": {}, "outputs": {}}
        try:
            from . import lineage
            lineage.report(inputs=refs["inputs"], outputs=refs["outputs"])
        except Exception:  # lineage must never fail a job
            pass


file_lineage = _FileLineage()


def _clean(path: str) -> str:
    path = path.lstrip("/")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError("paths may not contain '..'")
    return "/".join(parts)


class Files:
    """Files under one bucket/prefix of an S3-compatible data source.

    The prefix is enforced here for convenience; real isolation comes from the data source's
    credentials (admins should give each data source keys scoped to its bucket/prefix).
    """

    def __init__(self, info: Dict[str, Any]):
        import boto3
        from botocore.config import Config

        cfg, cred = info["config"], info["credentials"]
        self.name = info["name"]
        self.bucket = cfg["bucket"]
        self.prefix = _clean(cfg.get("prefix", ""))
        self.namespaces = (info.get("access") or {}).get("namespaces") or {}
        self.client = boto3.client(
            "s3", endpoint_url=cfg.get("endpoint_url"), region_name=cfg.get("region", "us-east-1"),
            aws_access_key_id=cred.get("access_key_id"), aws_secret_access_key=cred.get("secret_access_key"), aws_session_token=cred.get("session_token"),
            config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
        )

    def key(self, path: str) -> str:
        p = _clean(path)
        return f"{self.prefix}/{p}" if self.prefix and p else (self.prefix or p)

    def url(self, path: str = "") -> str:
        """s3:// URL for this object within the authorized prefix."""
        return f"s3://{self.bucket}/{self.key(path)}"

    def list(self, path: str = "", recursive: bool = True) -> List[Dict[str, Any]]:
        """Files below a folder: [{'path', 'size', 'modified'}], paths relative to the prefix."""
        base = self.key(path)
        if base and not base.endswith("/"):
            base += "/"
        out, token = [], None
        while True:
            kw: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": base}
            if not recursive:
                kw["Delimiter"] = "/"
            if token:
                kw["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                rel = o["Key"][len(self.prefix) + 1:] if self.prefix else o["Key"]
                out.append({"path": rel, "size": o["Size"], "modified": o["LastModified"].isoformat()})
            if not resp.get("IsTruncated"):
                return out
            token = resp["NextContinuationToken"]

    def exists(self, path: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key(path))
            return True
        except self.client.exceptions.ClientError:
            return False

    def _seen(self, path: str, field: str) -> None:
        file_lineage.note(self.name, self.namespaces, _clean(path), field)

    def read_bytes(self, path: str) -> bytes:
        data = self.client.get_object(Bucket=self.bucket, Key=self.key(path))["Body"].read()
        self._seen(path, "inputs")
        return data

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def read_json(self, path: str) -> Any:
        return json.loads(self.read_bytes(path))

    def read_jsonl(self, path: str) -> List[Any]:
        return [json.loads(line) for line in self.read_text(path).splitlines() if line.strip()]

    def write_bytes(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self.client.put_object(Bucket=self.bucket, Key=self.key(path), Body=data, ContentType=content_type)
        self._seen(path, "outputs")
        return self.url(path)

    def write_text(self, path: str, text: str, encoding: str = "utf-8") -> str:
        return self.write_bytes(path, text.encode(encoding), "text/plain; charset=utf-8")

    def write_json(self, path: str, obj: Any, indent: Optional[int] = 2) -> str:
        return self.write_bytes(path, json.dumps(obj, indent=indent, default=str).encode(), "application/json")

    def write_jsonl(self, path: str, rows: Iterable[Any]) -> str:
        return self.write_bytes(path, "".join(json.dumps(r, default=str) + "\n" for r in rows).encode(),
                                "application/x-ndjson")

    def read_csv(self, path: str, **kwargs: Any) -> Any:
        import pandas as pd

        return pd.read_csv(io.BytesIO(self.read_bytes(path)), **kwargs)

    def write_csv(self, path: str, df: Any, **kwargs: Any) -> str:
        return self.write_bytes(path, df.to_csv(index=False, **kwargs).encode(), "text/csv")

    def read_parquet(self, path: str) -> Any:
        import pandas as pd

        return pd.read_parquet(io.BytesIO(self.read_bytes(path)))

    def write_parquet(self, path: str, df: Any) -> str:
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        return self.write_bytes(path, buf.getvalue(), "application/vnd.apache.parquet")

    def download(self, path: str, local: str) -> str:
        self.client.download_file(self.bucket, self.key(path), local)
        self._seen(path, "inputs")
        return local

    def upload(self, local: str, path: str) -> str:
        self.client.upload_file(local, self.bucket, self.key(path))
        self._seen(path, "outputs")
        return self.url(path)

    def delete(self, path: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self.key(path))

    def __repr__(self) -> str:
        return f"<fdp.Files {self.name}: s3://{self.bucket}/{self.prefix}>"


def iceberg_catalog(info: Dict[str, Any]) -> Any:
    from pyiceberg.catalog import load_catalog

    cfg, cred = info["config"], info["credentials"]
    props = {
        "type": "rest", "uri": cfg["catalog_uri"],
        "s3.endpoint": cfg.get("s3_endpoint"), "s3.region": cfg.get("region", "us-east-1"),
        "s3.access-key-id": cred.get("access_key_id"), "s3.secret-access-key": cred.get("secret_access_key"),
        "s3.path-style-access": "true",
    }
    props["warehouse"] = _warehouse(info)
    if cred.get("catalog_token"):
        props["token"] = cred["catalog_token"]
    return load_catalog(info["name"], **{k: v for k, v in props.items() if v is not None})


def _warehouse(info: Dict[str, Any]) -> str:
    """The catalog warehouse to attach (REST 'warehouse' parameter); defaults to the source name."""
    w = info["config"].get("warehouse") or info["name"]
    return w[len("s3://"):].strip("/") if w.startswith("s3://") else w


class DelegatedFiles:
    """Viewer-local shared-app client. Every operation checks current server-side permissions."""
    def __init__(self, name: str, viewer_token: str):
        self.name, self.viewer_token = name, viewer_token

    def _request(self, operation, path="", **extra):
        from . import _client
        with _client() as client:
            response = client.post("/v1/delegated/files", headers={"X-FDP-Viewer": self.viewer_token},
                json={"source": self.name, "operation": operation, "path": path, **extra})
            response.raise_for_status()
            return response.json()

    def read_bytes(self, path):
        import base64
        return base64.b64decode(self._request("read", path)["data"])

    def read_text(self, path, encoding="utf-8"):
        return self.read_bytes(path).decode(encoding)

    def read_json(self, path):
        return json.loads(self.read_bytes(path))

    def write_bytes(self, path, data, content_type="application/octet-stream"):
        import base64
        self._request("write", path, data=base64.b64encode(data).decode(), content_type=content_type)

    def write_text(self, path, text, encoding="utf-8"):
        self.write_bytes(path, text.encode(encoding), "text/plain; charset=utf-8")

    def write_json(self, path, value, indent=2):
        self.write_text(path, json.dumps(value, indent=indent, default=str))

    def list(self, path=""):
        result = self._request("list", path)
        if result["truncated"]:
            raise RuntimeError("more than 1000 entries; choose a narrower prefix")
        return result["files"]

    def delete(self, path):
        self._request("delete", path)


    def read_csv(self, path, **kwargs):
        import pandas as pd
        return pd.read_csv(io.BytesIO(self.read_bytes(path)), **kwargs)

    def read_parquet(self, path, **kwargs):
        import pandas as pd
        return pd.read_parquet(io.BytesIO(self.read_bytes(path)), **kwargs)

    def write_csv(self, path, df, **kwargs):
        self.write_text(path, df.to_csv(index=False, **kwargs))

    def write_parquet(self, path, df, **kwargs):
        output = io.BytesIO()
        df.to_parquet(output, **kwargs)
        self.write_bytes(path, output.getvalue())
