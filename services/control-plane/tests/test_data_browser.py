import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select

from app import sql_access, storage_access
from app.db import SessionLocal
from app.models import DataSource, Resource, RoleAssignment
from app.routes.data_browser import storage_children
from .conftest import as_
from .test_shared_sql import sql_mode  # noqa: F401 -- shared execution fixture
from .rbac_helpers import grant_group


def schema_id(source="finance"):
    with SessionLocal() as db:
        source_id = db.scalar(select(DataSource.id).where(DataSource.name == source))
        return db.scalar(select(Resource.id).where(Resource.kind == "schema", Resource.object_id == source_id))


def test_sql_children_are_lazy_scoped_and_paginated(sql_mode, monkeypatch):
    client, _ = sql_mode
    sent = []

    async def engine(method, path, **kwargs):
        sent.append(kwargs["content"].decode())
        views = "information_schema.views" in sent[-1]
        return httpx.Response(200, json={"id": "metadata_test", "columns": [{"name": "table_name"}],
                                        "data": [["totals"]] if views else [["orders"], ["totals"]]})

    monkeypatch.setattr(sql_access, "engine_request", engine)
    # Listing registered resources must never query the data engine.
    client.get("/v1/rbac/resources", headers=as_("ana")).raise_for_status()
    assert not sent
    url = f"/v1/data/schemas/{schema_id()}/children"
    response = client.get(url, headers=as_("ana"), params={"limit": 1})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"children": [{"name": "orders", "kind": "table"}], "next_cursor": "orders"}
    assert len(sent) == 2 and '"finance".information_schema.views' in sent[0] and '"finance".information_schema.tables' in sent[1]
    assert "reporting" in sent[1]
    # Names only: table_type would make Trino load every Iceberg table's metadata.
    assert not any("table_type" in s for s in sent)
    response = client.get(url, headers=as_("ana"), params={"after": "orders", "limit": 5})
    assert response.json()["children"][1]["kind"] == "view"
    assert "orders" in sent[-1] and "LIMIT 6" in sent[-1]
    # The last listing comes back at once from the catalog index, without the engine.
    count = len(sent)
    quick = client.get(url, headers=as_("ana"), params={"cached": "true"})
    assert quick.status_code == 200, quick.text
    assert quick.json() == {"children": [{"name": "orders", "kind": "table"}, {"name": "totals", "kind": "view"}], "next_cursor": None, "cached": True}
    assert len(sent) == count
    # The same data grants apply to the cached listing.
    assert client.get(url, headers=as_("bob"), params={"cached": "true"}).status_code == 404


def test_columns_are_parameterized_and_do_not_read_table_rows(sql_mode, monkeypatch):
    client, _ = sql_mode
    sent = []

    async def engine(method, path, **kwargs):
        sent.append(kwargs["content"].decode())
        return httpx.Response(200, json={"id": "columns_test", "data": [["amount", "decimal(12,2)", "YES", 2]]})

    monkeypatch.setattr(sql_access, "engine_request", engine)
    table = "sales'; SELECT * FROM private.hidden --"
    url = f"/v1/data/schemas/{schema_id()}/children"
    response = client.get(url, headers=as_("ana"), params={"table": table, "after": "1"})
    assert response.status_code == 200, response.text
    assert response.json()["children"] == [{"name": "amount", "kind": "column", "data_type": "decimal(12,2)", "nullable": True, "ordinal_position": 2}]
    assert '"finance".information_schema.columns' in sent[0]
    assert "table_name = ?" in sent[0] and "sales''; SELECT" in sent[0]
    count = len(sent)
    assert client.get(url, headers=as_("ana"), params={"table": "sales", "after": "bad"}).status_code == 422
    assert client.get(url, headers=as_("ana"), params={"path": "somewhere"}).status_code == 422
    assert len(sent) == count


def test_browsing_requires_data_grants_and_ready_schema(sql_mode, monkeypatch):
    client, _ = sql_mode
    calls = []
    monkeypatch.setattr(sql_access, "engine_request", lambda *args, **kwargs: calls.append(args))
    rid = schema_id()
    url = f"/v1/data/schemas/{rid}/children"
    assert client.get(url, headers=as_("bob")).status_code == 404
    # Removing ordinary Owner assignments does not create data rights for other users.
    with SessionLocal() as db:
        db.execute(delete(RoleAssignment).where(RoleAssignment.role == "Owner"))
        db.commit()
    assert client.get(url, headers=as_('bob')).status_code == 404
    with SessionLocal() as db:
        item = db.get(Resource, rid)
        item.config = {**item.config, "state": "pending"}
        db.commit()
    assert client.get(url, headers=as_("ana")).status_code == 409
    assert client.get("/v1/data/schemas/missing/children", headers=as_("ana")).status_code == 404
    assert calls == []


def test_storage_listing_is_prefix_scoped_and_uses_read_only_sts(monkeypatch):
    import boto3
    source = SimpleNamespace(name="files", config={"bucket": "files", "prefix": "finance"})
    schema = SimpleNamespace(name="input", config={"prefix": "input"})
    calls = []

    def credentials(source, schemas, scope, store, db=None):
        assert scope == {"input": "Reader"}
        return {"access_key_id": "temporary", "secret_access_key": "temporary", "session_token": "scoped"}, {}

    class Storage:
        def list_objects_v2(self, **kwargs):
            calls.append(kwargs)
            return {"CommonPrefixes": [{"Prefix": "finance/input/2026/"}, {"Prefix": "private/hidden/"}],
                    "Contents": [{"Key": "finance/input/report.csv", "Size": 42}, {"Key": "private/hidden.csv"}],
                    "NextContinuationToken": "next"}

    def client(kind, **kwargs):
        assert kind == "s3" and kwargs["aws_session_token"] == "scoped"
        return Storage()

    monkeypatch.setattr(storage_access, "credentials", credentials)
    monkeypatch.setattr(boto3, "client", client)
    response = storage_children(None, source, schema, "", "", 50, None)
    assert calls == [{"Bucket": "files", "Prefix": "finance/input/", "Delimiter": "/", "MaxKeys": 50}]
    assert response["children"] == [{"name": "2026", "kind": "folder", "path": "2026"},
        {"name": "report.csv", "kind": "file", "path": "report.csv", "size": 42, "modified_at": None}]
    assert response["next_cursor"] == "next"
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        storage_children(None, source, schema, "../private", "", 50, None)
    with pytest.raises(HTTPException):
        storage_children(None, SimpleNamespace(config={"bucket": "lake"}), schema, "", "", 50, None, {"lake"})


@pytest.mark.skipif(not os.getenv("FDP_TEST_S3_URL"), reason="requires disposable RustFS")
def test_live_storage_browser_is_bounded_to_the_granted_namespace(client):
    import boto3
    from botocore.config import Config
    endpoint = os.environ["FDP_TEST_S3_URL"]
    cred = {"access_key_id": "fdp", "secret_access_key": "fdp-rustfs-secret"}
    raw = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id=cred["access_key_id"],
                       aws_secret_access_key=cred["secret_access_key"], config=Config(s3={"addressing_style": "path"}))
    bucket = "browser-" + uuid.uuid4().hex[:12]
    raw.create_bucket(Bucket=bucket)
    keys = ["finance/data/report.csv", "finance/data/2026/orders.csv", "private/hidden.csv"]
    try:
        for key in keys:
            raw.put_object(Bucket=bucket, Key=key, Body=b"test")
        client.put("/v1/admin/datasources/files", headers=as_("admin"), json={"name": "files", "kind": "s3",
            "config": {"endpoint_url": endpoint, "sts_endpoint_url": endpoint, "bucket": bucket, "prefix": "finance",
                       "role_arn": "arn:aws:iam::123456789012:role/fdp"}, "provisioner_secret": cred}).raise_for_status()
        schema = client.post("/v1/rbac/sources/files/schemas", headers=as_("admin"), json={"name": "input", "prefix": "data"}).json()
        client.post(f"/v1/rbac/resources/{schema['id']}/provision", headers=as_("admin")).raise_for_status()
        group = grant_group(client, schema["id"], "File readers", "Reader", users=("ana",))
        url = f"/v1/data/schemas/{schema['id']}/children"
        response = client.get(url, headers=as_("ana"))
        assert response.status_code == 200, response.text
        assert {c["name"] for c in response.json()["children"]} == {"report.csv", "2026"}
        response = client.get(url, params={"path": "2026"}, headers=as_("ana"))
        assert response.status_code == 200, response.text
        assert response.json()["children"][0]["path"] == "2026/orders.csv"
        assert "session_token" not in response.text and "secret_access_key" not in response.text
        assert client.get(url, params={"path": "../private"}, headers=as_("ana")).status_code == 422
        assert client.get(url, headers=as_("bob")).status_code == 404
        client.delete(f"/v1/rbac/resources/{schema['id']}/assignments/{group}", headers=as_("admin")).raise_for_status()
        assert client.get(url, headers=as_("ana")).status_code == 404
    finally:
        raw.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys]})
        raw.delete_bucket(Bucket=bucket)
