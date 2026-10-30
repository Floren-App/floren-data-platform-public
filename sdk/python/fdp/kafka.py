"""Kafka clients using FDP identity, TLS, token refresh and observed lineage."""
import json
import os
import ssl
import threading
import time


def _topic(ident, layer_id):
    from . import _client
    with _client() as client:
        response = client.get('/v1/kafka/resolve', params={'topic':ident,'layer_id':layer_id or ''})
        response.raise_for_status()
        return response.json()


class _Session:
    def __init__(self, topic, mode, group):
        self.topic, self.mode, self.group = topic, mode, group
        self.info, self.lock = {}, threading.Lock()

    def refresh(self):
        from . import _client
        with self.lock:
            if self.info.get('expires_at', 0) < time.time()+20:
                with _client() as client:
                    response = client.post('/v1/kafka/topics/'+self.topic['id']+'/session',
                                           json={'mode':self.mode,'group':self.group})
                    response.raise_for_status()
                    self.info = response.json()
            return self.info


class _Client:
    def __init__(self, raw, session):
        self._raw, self._session, self._last_observation = raw, session, None

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def _observe(self):
        if self._last_observation is not None and time.monotonic()-self._last_observation < 30:
            return
        self._last_observation = time.monotonic()
        try:
            from . import _client
            with _client() as client:
                client.post('/v1/kafka/topics/'+self._session.topic['id']+'/observations',
                            json={'mode':self._session.mode}).raise_for_status()
        except Exception:
            # Observability is best effort; broker authorization is not.
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._raw.close()


class Producer(_Client):
    def send(self, value=None, key=None, **kwargs):
        future = self._raw.send(self._session.topic['name'], value=value, key=key, **kwargs)
        future.add_callback(lambda _: self._observe())
        return future


class Consumer(_Client):
    def poll(self, *args, **kwargs):
        records = self._raw.poll(*args, **kwargs)
        if any(records.values()):
            self._observe()
        return records

    def __iter__(self):
        for record in self._raw:
            self._observe()
            yield record


def _create(ident, mode, group='', layer_id=None, **options):
    try:
        from kafka import KafkaProducer, KafkaConsumer
        from kafka.sasl.oauth import AbstractTokenProvider
    except ImportError:
        raise RuntimeError('Install the FDP Kafka extra: pip install "fdp[kafka]"') from None
    reserved = {'bootstrap_servers','security_protocol','sasl_mechanism','sasl_oauth_token_provider',
                'ssl_context','ssl_check_hostname','group_id'}
    if reserved.intersection(options):
        raise ValueError('FDP manages broker addresses, identity, consumer groups and TLS settings')
    session = _Session(_topic(ident, layer_id), mode, group)
    info = session.refresh()
    class Tokens(AbstractTokenProvider):
        def token(self):
            return session.refresh()['token']
    config = {'bootstrap_servers':info['bootstrap_servers'], 'security_protocol':'SASL_SSL',
              'sasl_mechanism':'OAUTHBEARER','sasl_oauth_token_provider':Tokens(),
              'ssl_context':ssl.create_default_context(cafile=os.environ.get('SSL_CERT_FILE')),
              **options}
    if mode == 'produce':
        config.setdefault('value_serializer', lambda value: json.dumps(value, separators=(',', ':')).encode())
        config.setdefault('acks', 'all')
        config.setdefault('enable_idempotence', True)
        result = Producer(KafkaProducer(**config), session)
    else:
        config.setdefault('enable_auto_commit', False)
        config.setdefault('value_deserializer', json.loads)
        result = Consumer(KafkaConsumer(info['topic'], group_id=info['group_id'], **config), session)
    from .streaming import ready
    ready()
    return result


def producer(topic, *, layer_id=None, **options):
    """JSON producer for one governed topic; .send(value).get(timeout=...) waits for acknowledgement."""
    return _create(topic, 'produce', layer_id=layer_id, **options)


def consumer(topic, *, group, layer_id=None, **options):
    """JSON consumer; commit only after durable processing. FDP prefixes group with your identity."""
    return _create(topic, 'consume', group=group, layer_id=layer_id, **options)
