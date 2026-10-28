import httpx

import fdp


def test_external_soap_call_keeps_program_identity_and_custom_headers(monkeypatch):
    seen = []
    transport = httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, text="<ok/>"))
    client = httpx.Client
    monkeypatch.setenv("FDP_API", "https://fdp.test")
    monkeypatch.setattr(fdp.httpx, "Client", lambda **kw: client(transport=transport, **kw))
    response = fdp.api("bank", token="program-token").soap("Balance", "<Balance/>", headers={"X-Request-Id": "req-1"})
    assert response.status_code == 200
    assert str(seen[0].url) == "https://fdp.test/v1/egress/bank/Balance"
    assert seen[0].headers["authorization"] == "Bearer program-token"
    assert seen[0].headers["x-request-id"] == "req-1"
    assert seen[0].content == b"<Balance/>"


def test_respond_writes_the_answer_and_its_status_for_an_internal_api_call(tmp_path, monkeypatch):
    import datetime
    import json
    import pytest
    monkeypatch.setenv("FDP_OUTPUT_DIR", str(tmp_path))
    fdp.respond({"orders": 42, "day": datetime.date(2026, 10, 23)})
    assert json.loads((tmp_path / "response.json").read_text()) == {"orders": 42, "day": "2026-10-23"}
    assert (tmp_path / "response.status").read_text() == "200"
    fdp.respond({"error": "unknown region"}, status=404)
    assert (tmp_path / "response.status").read_text() == "404"
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError):
        fdp.respond({}, status=204)
    with pytest.raises(ValueError):
        fdp.respond("x" * (fdp.RESPONSE_LIMIT + 1))
