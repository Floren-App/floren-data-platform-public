"""Preview the start of a file in an S3 files namespace: enough to see what it holds, never the whole file. Tables
(CSV, TSV, JSON lines, Parquet) show their first rows; JSON is formatted; text shows its first part; images show
as they are. The caller's own scoped credentials read it."""
import base64
import csv
import io
import json
from typing import Any, Dict, List

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from . import storage_access

ROWS = 25                       # rows shown for tables
TEXT_BYTES = 1024 * 1024        # bytes read for text, CSV, JSON
TEXT_SHOWN = 64 * 1024          # characters of text shown
PARQUET_BYTES = 50 * 1024 ** 2  # Parquet keeps its schema at the end: read whole up to this size
IMAGE_BYTES = 5 * 1024 ** 2

TABLES = {".csv": ",", ".tsv": "\t"}
JSON_LINES = (".jsonl", ".ndjson")
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
TEXT = (".txt", ".md", ".log", ".yaml", ".yml", ".xml", ".sql", ".py", ".r", ".sh", ".toml", ".ini", ".cfg", ".html")


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def _safe(value: Any) -> Any:
    """JSON-safe cell values: big integers as text, dates and bytes as text."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > 9007199254740991 else value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return value[:64].hex()
    return str(value)


def _read(client, bucket: str, key: str, limit: int) -> bytes:
    try:
        return client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{limit - 1}")["Body"].read()
    except (BotoCoreError, ClientError):
        raise HTTPException(502, "reading the file failed; check its permissions and the storage connection") from None


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _complete_lines(text: str, truncated: bool) -> List[str]:
    lines = text.splitlines()
    return lines[:-1] if truncated and lines else lines   # the last line may be cut off


def _csv(text: str, delimiter: str, truncated: bool) -> Dict[str, Any]:
    lines = _complete_lines(text, truncated)
    if delimiter == "," and lines:
        try:
            delimiter = csv.Sniffer().sniff("\n".join(lines[:20]), delimiters=",;\t|").delimiter
        except csv.Error:
            pass
    rows = list(csv.reader(lines[:ROWS + 1], delimiter=delimiter))
    header, body = (rows[0], rows[1:]) if rows else ([], [])
    width = max([len(header)] + [len(r) for r in body]) if rows else 0
    columns = [header[i] if i < len(header) and header[i] else f"column {i + 1}" for i in range(width)]
    return {"kind": "table", "columns": columns, "rows": [r + [None] * (width - len(r)) for r in body]}


def _json_lines(text: str, truncated: bool) -> Dict[str, Any]:
    records, columns = [], []
    for line in _complete_lines(text, truncated):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        record = record if isinstance(record, dict) else {"value": record}
        for key in record:
            if key not in columns:
                columns.append(key)
        records.append(record)
        if len(records) >= ROWS:
            break
    return {"kind": "table", "columns": columns, "rows": [[_safe(r.get(c)) for c in columns] for r in records]}


def _parquet(data: bytes) -> Dict[str, Any]:
    import pyarrow.parquet as pq
    try:
        handle = pq.ParquetFile(io.BytesIO(data))
        table = next(handle.iter_batches(batch_size=ROWS)) if handle.metadata.num_rows else None
    except Exception:
        raise HTTPException(422, "this file is not readable as Parquet") from None
    columns = [f.name for f in handle.schema_arrow]
    rows = [[_safe(row.get(c)) for c in columns] for row in table.to_pylist()] if table is not None else []
    return {"kind": "table", "columns": columns, "rows": rows, "total_rows": handle.metadata.num_rows,
            "types": [str(f.type) for f in handle.schema_arrow]}


def preview(client, bucket: str, root: str, path: str) -> Dict[str, Any]:
    relative = storage_access.prefix_path("", path)
    key = storage_access.prefix_path(root, relative)
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError):
        raise HTTPException(404, "file not found in this namespace") from None
    size, content_type = head.get("ContentLength", 0), head.get("ContentType") or ""
    ext = _extension(relative)
    out: Dict[str, Any] = {"path": relative, "size": size, "content_type": content_type, "truncated": False, "read": 0}
    if ext == ".parquet":
        if size > PARQUET_BYTES:
            return {**out, "kind": "none", "note": f"Parquet files over {PARQUET_BYTES // 1024 ** 2} MB are not previewed; read it in code."}
        data = _read(client, bucket, key, size or 1)
        return {**out, **_parquet(data), "read": len(data)}
    if ext in IMAGES:
        if size > IMAGE_BYTES:
            return {**out, "kind": "none", "note": f"Images over {IMAGE_BYTES // 1024 ** 2} MB are not previewed."}
        data = _read(client, bucket, key, size or 1)
        return {**out, "kind": "image", "content_type": IMAGES[ext], "data": base64.b64encode(data).decode(), "read": len(data)}
    data = _read(client, bucket, key, TEXT_BYTES)
    truncated = size > len(data)
    out.update(read=len(data), truncated=truncated)
    if b"\x00" in data[:8192] and ext not in TABLES and ext not in JSON_LINES and ext not in TEXT and ext != ".json":
        return {**out, "kind": "none", "note": "This is a binary file; read it in code."}
    text = _text(data)
    if ext in TABLES:
        return {**out, **_csv(text, TABLES[ext], truncated)}
    if ext in JSON_LINES:
        return {**out, **_json_lines(text, truncated)}
    if ext == ".json" and not truncated:
        try:
            formatted = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            return {**out, "kind": "json", "text": formatted[:TEXT_SHOWN], "truncated": len(formatted) > TEXT_SHOWN}
        except ValueError:
            pass
    return {**out, "kind": "text", "text": text[:TEXT_SHOWN], "truncated": truncated or len(text) > TEXT_SHOWN}
