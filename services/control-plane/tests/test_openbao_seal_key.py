"""The bundled OpenBao's static seal key: created once, reused, never rewritten, never logged."""
import base64

import pytest
from kubernetes.client.rest import ApiException

from app.openbao_seal_key import KEY_BYTES, ensure_key


class FakeCoreV1:
    def __init__(self, existing=None, race=False):
        self.secrets, self.created, self.race = {}, [], race
        if existing is not None:
            self.secrets['fdp-openbao-seal'] = existing

    def read_namespaced_secret(self, name, namespace):
        if name not in self.secrets:
            raise ApiException(status=404)
        return self.secrets[name]

    def create_namespaced_secret(self, namespace, body):
        if self.race:   # another pod created it between our read and create
            self.secrets[body.metadata.name] = type('S', (), {'data': {'key': base64.b64encode(b'k' * KEY_BYTES).decode()}})()
            raise ApiException(status=409)
        self.created.append(body)
        self.secrets[body.metadata.name] = body


def test_a_new_key_is_created_once_as_an_immutable_secret():
    api = FakeCoreV1()
    key = ensure_key(api, 'fdp-openbao', 'fdp-openbao-seal')
    assert len(key) == KEY_BYTES
    assert api.created[0].immutable is True and api.created[0].type == 'Opaque'
    assert ensure_key(api, 'fdp-openbao', 'fdp-openbao-seal') == key
    assert len(api.created) == 1


def test_an_existing_key_is_reused_and_a_concurrent_creation_wins():
    existing = type('S', (), {'data': {'key': base64.b64encode(b'x' * KEY_BYTES).decode()}})()
    assert ensure_key(FakeCoreV1(existing), 'ns', 'fdp-openbao-seal') == b'x' * KEY_BYTES
    assert ensure_key(FakeCoreV1(race=True), 'ns', 'fdp-openbao-seal') == b'k' * KEY_BYTES


def test_a_key_of_the_wrong_length_is_refused():
    short = type('S', (), {'data': {'key': base64.b64encode(b'short').decode()}})()
    with pytest.raises(SystemExit, match='32-byte key'):
        ensure_key(FakeCoreV1(short), 'ns', 'fdp-openbao-seal')
