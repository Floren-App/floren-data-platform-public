import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

import fdp
from fdp import kafka


def test_client_scopes_identity_refreshes_tokens_and_records_only_acknowledged_messages(monkeypatch):
    import kafka as native
    requests=[]
    sessions=[]
    callbacks=[]
    def respond(request):
        requests.append(request)
        if request.url.path=='/v1/kafka/resolve':
            return httpx.Response(200,json={'id':'topic-id','name':'orders'})
        if request.url.path.endswith('/session'):
            sessions.append(json.loads(request.content))
            return httpx.Response(200,json={'token':f'token-{len(sessions)}','expires_at':10**11,
                'topic':'orders','bootstrap_servers':'broker.test:9093','group_id':'identity.billing'})
        return httpx.Response(201,json={'recorded':True})
    @contextmanager
    def client():
        with httpx.Client(base_url='https://fdp.test',transport=httpx.MockTransport(respond),
                          headers={'authorization':'Bearer execution-token'}) as value:
            yield value
    monkeypatch.setattr(fdp,'_client',client)
    class Raw:
        def __init__(self,**config):self.config=config
        def send(self,topic,**kwargs):
            assert topic=='orders'
            return SimpleNamespace(add_callback=callbacks.append)
        def close(self):pass
    monkeypatch.setattr(native,'KafkaProducer',Raw)
    producer=kafka.producer('orders',layer_id='non-prod')
    assert sessions==[{'mode':'produce','group':''}]
    assert producer.config['security_protocol']=='SASL_SSL'
    assert producer.config['enable_idempotence'] is True and producer.config['acks']=='all'
    assert producer.config['ssl_context'].check_hostname
    assert producer.config['value_serializer']({'id':1})==b'{"id":1}'
    provider=producer.config['sasl_oauth_token_provider']
    assert provider.token()=='token-1'
    producer._session.info['expires_at']=0
    assert provider.token()=='token-2'
    producer.send({'id':1})
    assert not any(r.url.path.endswith('/observations') for r in requests)
    callbacks[0](None)
    observations=[r for r in requests if r.url.path.endswith('/observations')]
    assert len(observations)==1
    assert all(r.headers['authorization']=='Bearer execution-token' for r in requests)


@pytest.mark.parametrize('setting',['bootstrap_servers','security_protocol','group_id','ssl_check_hostname','ssl_context','sasl_oauth_token_provider'])
def test_callers_cannot_replace_platform_identity_network_or_tls(setting):
    pytest.importorskip('kafka')
    with pytest.raises(ValueError,match='FDP manages'):
        kafka.producer('orders',**{setting:'forbidden'})


def test_consumer_observation_requires_actual_messages():
    session=SimpleNamespace(topic={'id':'topic-id'},mode='consume')
    raw=SimpleNamespace(poll=lambda **kwargs:{},close=lambda:None)
    consumer=kafka.Consumer(raw,session)
    consumer._observe=lambda:pytest.fail('Empty polls must not report observed data')
    assert consumer.poll(timeout_ms=1)=={}
