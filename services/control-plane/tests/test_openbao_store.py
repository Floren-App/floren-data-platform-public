"""OpenBao failures reach callers as sanitized 503s, never provider responses or secret material."""
from types import SimpleNamespace

import hvac
import pytest
import requests

from app.secrets import OpenBaoSecretStore, SecretBackendError
from .conftest import as_


def store_raising(exc):
    def fail(**_):
        raise exc
    kv = SimpleNamespace(create_or_update_secret=fail, read_secret_version=fail, delete_metadata_and_all_versions=fail)
    store = OpenBaoSecretStore.__new__(OpenBaoSecretStore)
    store.mount = 'fdp'
    store.client = SimpleNamespace(secrets=SimpleNamespace(kv=SimpleNamespace(v2=kv)))
    return store


@pytest.mark.parametrize('exc,message', [
    (hvac.exceptions.VaultDown('Vault is sealed: token s.SECRET'), 'sealed'),
    (hvac.exceptions.Forbidden('permission denied on fdp/data/env/x'), 'refused the platform token'),
    (requests.exceptions.ConnectionError('http://openbao:8200 refused'), 'Cannot reach OpenBao'),
    (hvac.exceptions.InternalServerError('backend detail SECRET'), 'rejected the request'),
])
def test_backend_failures_are_sanitized(exc, message):
    store = store_raising(exc)
    for call in (lambda: store.put('env/x', {'value': 'SECRET'}), lambda: store.read('env/x')):
        with pytest.raises(SecretBackendError) as error:
            call()
        assert message in str(error.value) and 'SECRET' not in str(error.value) and 'openbao:8200' not in str(error.value)


def test_missing_paths_are_not_errors():
    store = store_raising(hvac.exceptions.InvalidPath())
    assert store.read('env/missing') == {}
    store.delete('env/missing')


def test_a_sealed_backend_is_a_503_for_api_callers(client, monkeypatch):
    from app import deps
    sealed = store_raising(hvac.exceptions.VaultDown('sealed'))
    client.app.dependency_overrides[deps.get_secret_store] = lambda: sealed
    try:
        response = client.put('/v1/secrets/user/ana/token', headers=as_('ana'), json={'value': 'x'})
    finally:
        client.app.dependency_overrides.pop(deps.get_secret_store, None)
    assert response.status_code == 503 and 'unseal' in response.json()['detail']
