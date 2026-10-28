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


class FakeVault:
    """An hvac-like client: login issues tokens; a request with a refused token raises Forbidden."""

    def __init__(self, lease=3600):
        self.token, self.logins, self.valid, self.data, self.lease = None, [], set(), {}, lease
        self.auth = SimpleNamespace(kubernetes=SimpleNamespace(login=self._login('kubernetes')),
                                    approle=SimpleNamespace(login=self._login('approle')))
        kv = SimpleNamespace(create_or_update_secret=self._put, read_secret_version=self._read,
                             delete_metadata_and_all_versions=lambda path, mount_point: self.data.pop(path, None))
        self.secrets = SimpleNamespace(kv=SimpleNamespace(v2=kv))

    def _login(self, method):
        def login(**kw):
            if self.token is not None and self.token not in self.valid:   # like the real servers
                raise hvac.exceptions.Forbidden('permission denied')
            self.logins.append((method, kw))
            self.token = f'token-{len(self.logins)}'
            self.valid.add(self.token)
            return {'auth': {'client_token': self.token, 'lease_duration': self.lease}}
        return login

    def _check(self):
        if self.token not in self.valid:
            raise hvac.exceptions.Forbidden('permission denied')

    def _put(self, path, secret, mount_point):
        self._check()
        self.data[path] = dict(secret)

    def _read(self, path, mount_point, raise_on_deleted_version):
        self._check()
        if path not in self.data:
            raise hvac.exceptions.InvalidPath()
        return {'data': {'data': self.data[path]}}


def vault_store(tmp_path, auth='kubernetes', backend='openbao', lease=3600):
    from app.config import Settings
    jwt = tmp_path / 'token'
    jwt.write_text('service-account-jwt\n')
    store = OpenBaoSecretStore(Settings(secrets_backend=backend, openbao_auth=auth, kubernetes_token_file=str(jwt),
                                        openbao_auth_role='fdp-control-plane', openbao_approle_role_id='rid',
                                        openbao_approle_secret_id='sid'))
    store.client = FakeVault(lease)
    return store


def test_kubernetes_auth_logs_in_with_the_service_account_token(tmp_path):
    store = vault_store(tmp_path)
    store.put('env/x', {'value': 'v'})
    assert store.read('env/x') == {'value': 'v'}
    assert store.client.logins == [('kubernetes', {'role': 'fdp-control-plane', 'jwt': 'service-account-jwt',
                                                   'mount_point': 'kubernetes'})]


def test_a_refused_login_token_is_replaced_once(tmp_path):
    store = vault_store(tmp_path, auth='approle')
    store.put('env/x', {'value': 'v'})
    store.client.valid.clear()   # revoked or expired on the server
    assert store.read('env/x') == {'value': 'v'}
    assert [m for m, _ in store.client.logins] == ['approle', 'approle']
    assert store.client.logins[0][1] == {'role_id': 'rid', 'secret_id': 'sid', 'mount_point': 'approle'}


def test_login_tokens_are_renewed_before_they_expire(tmp_path, monkeypatch):
    import time
    store = vault_store(tmp_path, lease=100)
    store.read('env/missing')
    now = time.monotonic()
    monkeypatch.setattr(time, 'monotonic', lambda: now + 81)   # past 80% of the lease
    store.read('env/missing')
    assert len(store.client.logins) == 2


def test_a_persistently_refused_identity_is_a_sanitized_503(tmp_path):
    store = vault_store(tmp_path, backend='vault')
    store.client._check = lambda: (_ for _ in ()).throw(hvac.exceptions.Forbidden('denied: token s.SECRET'))
    with pytest.raises(SecretBackendError) as error:
        store.read('env/x')
    assert str(error.value).startswith('Vault refused') and 'SECRET' not in str(error.value)


def test_a_missing_service_account_token_is_explained(tmp_path):
    store = vault_store(tmp_path)
    store.jwt_file = str(tmp_path / 'absent')
    with pytest.raises(SecretBackendError, match='service account token is unavailable'):
        store.read('env/x')


def test_only_the_configured_identity_is_used(tmp_path, monkeypatch):
    """hvac would otherwise pick up VAULT_TOKEN or ~/.vault-token and silently act as that identity."""
    from app.config import Settings
    monkeypatch.setenv('VAULT_TOKEN', 'stray-root-token')
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.vault-token').write_text('stray-file-token')
    assert OpenBaoSecretStore(Settings(secrets_backend='vault', openbao_auth='kubernetes')).client.token is None
    assert OpenBaoSecretStore(Settings(secrets_backend='vault', openbao_token='configured')).client.token == 'configured'
