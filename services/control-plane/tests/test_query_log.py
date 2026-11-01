"""The query log: Trino's completed-query events, joined with who or what ran them; and the metrics."""
from app import deps, layers, query_log
from app.db import SessionLocal
from app.layer_infrastructure import namespace_for
from app.layer_manifests import EVENTS_SECRET
from app.models import SqlQuery, utcnow
from .conftest import as_

ADMIN = as_("admin")
LAYER = layers.NON_PROD
TOKEN = "layer-events-token"


def events_token():
    from app.config import get_settings
    namespace = namespace_for(LAYER, get_settings().layer_namespace_prefix or "fdp-layer")
    deps.get_orchestrator().layer_secrets[(namespace, EVENTS_SECRET)] = ("", {"events-token": TOKEN})
    query_log._tokens.clear()


def event(query_id, trace="", state="FINISHED", sql="SELECT * FROM orders WHERE email = 'a@b.c'", **extra):
    return {"metadata": {"queryId": query_id, "query": sql, "queryState": state},
            "statistics": {"cpuTime": 0.25, "wallTime": "PT1.5S", "queuedTime": "12.00ms", "outputRows": 42, "totalBytes": 2048},
            "context": {"user": "p-trino-user", "traceToken": trace, "source": "fdp", "queryType": "SELECT"},
            "ioMetadata": {"inputs": [{"catalogName": "lake", "schema": "sales", "table": "orders"}]},
            "createTime": "2026-10-25T10:00:00.000Z", "endTime": "2026-10-25T10:00:01.500Z", **extra}


def admitted(principal, actor, **context):
    with SessionLocal() as db:
        row = SqlQuery(principal_id=principal, actor=actor, binding="b", scope={}, expires_at=utcnow(), **context)
        db.add(row)
        db.commit()
        return row.id


def post(client, body, token=TOKEN):
    return client.post(f"/internal/trino/layers/{LAYER}/events", json=body, headers={"Authorization": f"Bearer {token}"})


def test_events_are_accepted_only_with_the_layers_token(client):
    assert post(client, event("q0")).status_code == 401          # no token in the layer yet
    events_token()
    assert post(client, event("q0"), token="forged").status_code == 401
    assert client.post(f"/internal/trino/layers/{LAYER}/events", json=event("q0")).status_code == 401
    assert post(client, event("q0")).status_code == 204
    assert post(client, event("q0")).status_code == 204           # the same query twice: stored once
    assert len(client.get("/v1/query-log", headers=ADMIN).json()) == 1


def test_the_log_says_who_or_what_ran_each_query_and_keeps_sql_only_when_asked(client):
    events_token()
    ana = client.get("/v1/me", headers=as_("ana")).json()["principal_id"]
    bob = client.get("/v1/me", headers=as_("bob")).json()["principal_id"]
    post(client, event("q1", trace=admitted(ana, "ana", run_id="run1", workspace_id="ws1")))
    post(client, event("q2", trace=admitted(bob, "service:nightly", workload_id="wl1"), state="FAILED",
                       failureInfo={"errorCode": {"name": "TABLE_NOT_FOUND"}, "failureMessage": "Table lake.sales.x does not exist"}))
    rows = {r["query_id"]: r for r in client.get("/v1/query-log", headers=ADMIN).json()}
    q1, q2 = rows["q1"], rows["q2"]
    assert (q1["actor"], q1["run_id"], q1["workspace_id"], q1["tables"]) == ("ana", "run1", "ws1", ["lake.sales.orders"])
    assert (q1["rows"], q1["bytes"], q1["cpu_ms"], q1["wall_ms"], q1["queued_ms"]) == (42, 2048, 250, 1500, 12)
    assert q1["sql_text"] is None                                  # off by default: SQL can hold literal values
    assert (q2["actor"], q2["workload_id"], q2["state"]) == ("service:nightly", "wl1", "FAILED")
    assert q2["error"].startswith("TABLE_NOT_FOUND")
    # Own-query inspection is an explicit page gate plus a principal-scoped capability.
    from .test_permissions import grant, page
    page(client, 'set-querylog')
    grant(client, target_id=bob, namespace='query_log', layer_id=None, role='Viewer')
    client.post('/v1/permissions/assignments', headers=ADMIN, json={
        'target_id': 'set-querylog', 'namespace': 'pages', 'principal_id': ana, 'role': 'Viewer'}).raise_for_status()
    client.post('/v1/permissions/assignments', headers=ADMIN, json={
        'target_id': ana, 'namespace': 'query_log', 'principal_id': ana, 'role': 'Viewer'}).raise_for_status()
    assert [r["query_id"] for r in client.get("/v1/query-log", headers=as_("ana")).json()] == ["q1"]
    assert {r["query_id"] for r in client.get("/v1/query-log?state=failed", headers=ADMIN).json()} == {"q2"}
    assert {r["query_id"] for r in client.get("/v1/query-log?table=orders", headers=ADMIN).json()} == {"q1", "q2"}
    # With SQL text turned on, new queries keep it.
    assert client.put("/v1/admin/settings/query-log", headers=as_("ana"), json={"retention_days": 30, "record_sql": True}).status_code == 403
    assert client.put("/v1/admin/settings/query-log", headers=ADMIN, json={"retention_days": 30, "record_sql": True}).json() == {
        "retention_days": 30, "record_sql": True}
    post(client, event("q3"))
    q3 = next(r for r in client.get("/v1/query-log", headers=ADMIN).json() if r["query_id"] == "q3")
    assert q3["sql_text"].startswith("SELECT") and q3["actor"] == "p-trino-user"   # no admission record: Trino's user
    assert client.delete("/v1/admin/settings/query-log", headers=ADMIN).status_code == 204
    assert client.get("/v1/admin/settings/query-log", headers=ADMIN).json() == {"retention_days": 90, "record_sql": False}


def test_rows_past_retention_are_removed(client):
    events_token()
    post(client, event("old", createTime="2020-01-01T00:00:00Z"))
    post(client, event("new", createTime=utcnow().isoformat()))
    client.put("/v1/admin/settings/query-log", headers=ADMIN, json={"retention_days": 1, "record_sql": False})
    with SessionLocal() as db:
        assert query_log.prune(db) == 1
        db.commit()
    assert [r["query_id"] for r in client.get("/v1/query-log", headers=ADMIN).json()] == ["new"]


def test_durations_and_times_in_every_format_trino_may_use():
    assert [query_log.millis(v) for v in (1.5, "PT1.5S", "PT2M", "1.50s", "250.00ms", "3m", None, "x")] == [1500, 1500, 120000, 1500, 250, 180000, 0, 0]
    assert query_log.timestamp("2026-10-25T10:00:00Z").hour == 10 and query_log.timestamp(1761386400000).year == 2025


def test_metrics_report_requests_and_platform_state_with_the_token(client, monkeypatch):
    from app.config import get_settings
    assert client.get("/internal/metrics").status_code == 404                     # no token configured
    monkeypatch.setattr(get_settings(), "metrics_token", "m")
    assert client.get("/internal/metrics", headers={"Authorization": "Bearer x"}).status_code == 401
    client.get("/v1/me", headers=ADMIN)
    text = client.get("/internal/metrics", headers={"Authorization": "Bearer m"}).text
    assert 'fdp_leader 1' in text and 'fdp_http_requests_total{method="GET",route="/v1/me",status="200"}' in text
    assert "fdp_http_request_duration_seconds_bucket" in text and "# TYPE fdp_build_info gauge" in text
    assert 'fdp_warehouse_in_use{warehouse="small"} 0' in text
