"""Previews of files in S3 files namespaces: the start of a file, read with the caller's scoped credentials."""
import io
import json

import pytest
from fastapi import HTTPException

from app.file_preview import ROWS, TEXT_BYTES, preview


class Storage:
    """A bucket in memory that honours byte ranges, like S3."""
    def __init__(self, objects):
        self.objects, self.reads = objects, []

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise __import__("botocore.exceptions", fromlist=["ClientError"]).ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key]), "ContentType": "application/octet-stream"}

    def get_object(self, Bucket, Key, Range):
        start, end = (int(x) for x in Range.removeprefix("bytes=").split("-"))
        self.reads.append((Key, end - start + 1))
        return {"Body": io.BytesIO(self.objects[Key][start:end + 1])}


def test_tables_show_their_first_rows():
    csv = "region;amount\n" + "\n".join(f"r{i};{i}" for i in range(100))
    lines = "\n".join(json.dumps({"id": i, "tags": ["a"], **({"extra": True} if i == 2 else {})}) for i in range(50))
    store = Storage({"ns/sales.csv": csv.encode(), "ns/events.jsonl": lines.encode()})
    out = preview(store, "files", "ns", "sales.csv")
    assert out["kind"] == "table" and out["columns"] == ["region", "amount"] and len(out["rows"]) == ROWS and out["rows"][0] == ["r0", "0"]
    out = preview(store, "files", "ns", "events.jsonl")
    assert out["columns"] == ["id", "tags", "extra"] and out["rows"][2] == [2, ["a"], True] and out["rows"][0][2] is None


def test_parquet_shows_rows_and_types():
    import pyarrow as pa
    import pyarrow.parquet as pq
    sink = io.BytesIO()
    pq.write_table(pa.table({"card": [f"c{i}" for i in range(40)], "price": [i * 1.5 for i in range(40)]}), sink)
    out = preview(Storage({"ns/cards.parquet": sink.getvalue()}), "files", "ns", "cards.parquet")
    assert out["kind"] == "table" and out["columns"] == ["card", "price"] and out["total_rows"] == 40
    assert out["types"] == ["string", "double"] and out["rows"][1] == ["c1", 1.5] and len(out["rows"]) == ROWS


def test_json_text_images_and_limits():
    big = b"x" * (TEXT_BYTES * 2)
    store = Storage({"ns/config.json": b'{"a":{"b":[1,2]}}', "ns/notes.log": big, "ns/logo.png": b"\x89PNG...", "ns/blob.bin": b"\x00\x01\x02"})
    assert preview(store, "files", "ns", "config.json")["text"] == json.dumps({"a": {"b": [1, 2]}}, indent=2)
    log = preview(store, "files", "ns", "notes.log")
    assert log["kind"] == "text" and log["truncated"] and store.reads[-1] == ("ns/notes.log", TEXT_BYTES)   # never the whole file
    image = preview(store, "files", "ns", "logo.png")
    assert image["kind"] == "image" and image["content_type"] == "image/png" and image["data"]
    assert preview(store, "files", "ns", "blob.bin")["kind"] == "none"
    with pytest.raises(HTTPException) as missing:
        preview(store, "files", "ns", "absent.csv")
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException):
        preview(store, "files", "ns", "../other/secret.csv")   # never outside the namespace
