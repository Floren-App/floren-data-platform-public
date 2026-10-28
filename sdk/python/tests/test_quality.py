import json
import httpx
import fdp


def test_measurement_helpers_keep_workload_auth_and_idempotency(monkeypatch):
    seen=[]
    transport=httpx.MockTransport(lambda request:seen.append(request) or httpx.Response(200,json={'ok':True}))
    original=httpx.Client
    monkeypatch.setenv('FDP_API','https://fdp.test')
    monkeypatch.setattr(fdp.httpx,'Client',lambda **kwargs:original(transport=transport,**kwargs))
    fdp.measure_metric('dataset','coverage',token='execution-token')
    fdp.report_metric('dataset','coverage',98.7,numerator=987,denominator=1000,idempotency_key='batch-1',token='execution-token')
    fdp.observe_dataset('dataset',kind='checked',idempotency_key='batch-1',token='execution-token')
    assert len(seen)==3 and all(r.headers['authorization']=='Bearer execution-token' for r in seen)
    assert seen[0].url.path.endswith('/metrics/coverage/measure')
    assert json.loads(seen[1].content)['denominator']==1000
    assert json.loads(seen[2].content)=={'kind':'checked','idempotency_key':'batch-1'}
