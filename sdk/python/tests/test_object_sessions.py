import json
import httpx
import pytest
import fdp
from fdp import lake


def test_object_files_uses_only_server_issued_scope(monkeypatch):
    requests = []
    info = {'name':'files', 'kind':'s3', 'config':{'bucket':'customer', 'prefix':'dev/card.json'},
            'credentials':{'access_key_id':'short-lived', 'secret_access_key':'session'}}
    transport = httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json=info))
    client = httpx.Client
    monkeypatch.setenv('FDP_API', 'https://fdp.test')
    monkeypatch.setattr(fdp.httpx, 'Client', lambda **kwargs: client(transport=transport, **kwargs))
    monkeypatch.setattr(lake, 'Files', lambda data:data)
    assert fdp.files_object('abc123', token='job-token') == info
    request = requests[0]
    assert str(request.url) == 'https://fdp.test/v1/data-objects/abc123/session'
    assert request.headers['authorization'] == 'Bearer job-token'
    assert json.loads(request.content) == {'mode':'read'}


def test_refused_object_session_has_no_native_fallback(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    client = httpx.Client
    monkeypatch.setenv('FDP_API', 'https://fdp.test')
    monkeypatch.setattr(fdp.httpx, 'Client', lambda **kwargs: client(transport=transport, **kwargs))
    with pytest.raises(httpx.HTTPStatusError):
        fdp.files_object('abc123', mode='write', token='job-token')
