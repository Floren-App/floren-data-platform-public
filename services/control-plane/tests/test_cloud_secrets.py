"""Cloud secret managers: the same put/read/delete contract as OpenBao, provider-safe names, sanitized errors.

AWS runs against moto's in-process Secrets Manager. Azure Key Vault and Google Secret Manager use fakes that
mirror their SDK behavior (soft delete, versions) and raise the SDKs' own exception types.
"""
import re
from types import SimpleNamespace

import pytest

from app.cloud_secrets import AwsSecretStore, AzureSecretStore, GcpSecretStore, provider_name
from app.config import Settings
from app.secrets import SecretBackendError, make_secret_store

PATHS = ['datasources/sales', 'env/user/ana.lopez@example.com/api-token', 'airflow/1c9125f0',
         'env/group/' + 'g' * 300 + '/key']


@pytest.mark.parametrize('limit,allowed,pattern', [(127, 'A-Za-z0-9-', r'[A-Za-z0-9-]{1,127}'),
                                                   (255, 'A-Za-z0-9_-', r'[A-Za-z0-9_-]{1,255}')])
def test_provider_names_are_valid_unique_and_stable(limit, allowed, pattern):
    names = [provider_name('fdp-', path, limit, allowed) for path in PATHS + ['env/user/ana/x', 'env/user/ana-x']]
    assert all(re.fullmatch(pattern, name) for name in names)
    assert len(set(names)) == len(names)
    assert provider_name('fdp-', PATHS[0], limit, allowed) == names[0]


# --- AWS Secrets Manager (moto) ------------------------------------------------------------------

@pytest.fixture
def aws(monkeypatch):
    moto = pytest.importorskip('moto')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'eu-west-1')
    with moto.mock_aws():
        def make(**kw):
            store = AwsSecretStore(Settings(secrets_backend='aws', aws_secrets_region='eu-west-1', **kw))
            store.client = AwsDeletionSemantics(store.client)
            return store
        yield make


class AwsDeletionSemantics:
    """moto accepts writes to a secret scheduled for deletion; AWS rejects them with InvalidRequestException."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def put_secret_value(self, **kw):
        from botocore.exceptions import ClientError
        try:
            deleted = 'DeletedDate' in self._client.describe_secret(SecretId=kw['SecretId'])
        except ClientError:
            deleted = False
        if deleted:
            raise ClientError({'Error': {'Code': 'InvalidRequestException', 'Message': 'marked for deletion'}},
                              'PutSecretValue')
        return self._client.put_secret_value(**kw)


def test_aws_round_trip_and_reuse_of_a_deleted_path(aws):
    store = aws()
    for path in PATHS:
        assert store.read(path) == {}
        store.put(path, {'username': 'u', 'password': 'p'})
        store.put(path, {'username': 'u', 'password': 'rotated'})
        assert store.read(path) == {'username': 'u', 'password': 'rotated'}
    store.delete(PATHS[0])
    assert store.read(PATHS[0]) == {}
    store.delete(PATHS[0])   # deleting again is not an error
    store.put(PATHS[0], {'password': 'again'})   # restored inside its recovery window, then overwritten
    assert store.read(PATHS[0]) == {'password': 'again'}


def test_aws_names_tags_and_immediate_delete(aws):
    store = aws(aws_secrets_recovery_window_days=0, aws_secrets_prefix='fdp/')
    store.put('datasources/sales', {'password': 'p'})
    described = store.client.describe_secret(SecretId='fdp/datasources/sales')
    assert {'Key': 'managed-by', 'Value': 'fdp'} in described['Tags']
    store.delete('datasources/sales')
    assert store.client.list_secrets()['SecretList'] == []
    with pytest.raises(ValueError):
        aws(aws_secrets_recovery_window_days=3)


def test_aws_errors_are_sanitized():
    from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

    def failing(exc):
        def call(**_):
            raise exc
        return SimpleNamespace(get_secret_value=call, put_secret_value=call, delete_secret=call)
    denied = ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'arn:aws:secretsmanager:SECRET'}}, 'GetSecretValue')
    cases = [(denied, 'refused the platform identity'),
             (EndpointConnectionError(endpoint_url='https://secretsmanager.internal'), 'Cannot reach'),
             (NoCredentialsError(), 'no platform identity'),
             (ClientError({'Error': {'Code': 'InternalServiceError', 'Message': 'SECRET'}}, 'x'), 'rejected')]
    for exc, message in cases:
        store = AwsSecretStore(Settings(secrets_backend='aws'), client=failing(exc))
        for call in (lambda: store.read('x'), lambda: store.put('x', {'v': 'SECRET'})):
            with pytest.raises(SecretBackendError) as error:
                call()
            assert message in str(error.value) and 'SECRET' not in str(error.value) and 'internal' not in str(error.value)


# --- Azure Key Vault (fake SecretClient) ----------------------------------------------------------

class FakeKeyVault:
    """Soft delete like Key Vault: a deleted name cannot be set again until it is recovered."""

    def __init__(self):
        self.live, self.deleted = {}, {}

    def set_secret(self, name, value, content_type=None, tags=None):
        from azure.core.exceptions import ResourceExistsError
        assert re.fullmatch(r'[A-Za-z0-9-]{1,127}', name) and content_type == 'application/json'
        if name in self.deleted:
            error = ResourceExistsError('Secret is currently in a deleted but recoverable state')
            error.status_code = 409
            raise error
        self.live[name] = value

    def get_secret(self, name):
        from azure.core.exceptions import ResourceNotFoundError
        if name not in self.live:
            raise ResourceNotFoundError('SecretNotFound')
        return SimpleNamespace(value=self.live[name])

    def begin_delete_secret(self, name):
        from azure.core.exceptions import ResourceNotFoundError
        if name not in self.live:
            raise ResourceNotFoundError('SecretNotFound')
        self.deleted[name] = self.live.pop(name)
        return SimpleNamespace(wait=lambda: None)

    def begin_recover_deleted_secret(self, name):
        self.live[name] = self.deleted.pop(name)
        return SimpleNamespace(wait=lambda: None)


def azure(client):
    return AzureSecretStore(Settings(secrets_backend='azure', azure_key_vault_url='https://fdp.vault.azure.net/'),
                            client=client)


def test_azure_round_trip_soft_delete_and_recovery():
    pytest.importorskip('azure.core')
    store = azure(FakeKeyVault())
    for path in PATHS:
        store.put(path, {'token': 't'})
        assert store.read(path) == {'token': 't'}
    store.delete(PATHS[1])
    assert store.read(PATHS[1]) == {}
    store.delete(PATHS[1])
    store.put(PATHS[1], {'token': 'new'})   # recovered, then overwritten
    assert store.read(PATHS[1]) == {'token': 'new'}
    with pytest.raises(SecretBackendError, match='25 KB'):
        store.put('big', {'pem': 'x' * 30000})


def test_azure_errors_are_sanitized():
    from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ServiceRequestError

    def failing(exc):
        def call(*_, **__):
            raise exc
        return SimpleNamespace(get_secret=call, set_secret=call, begin_delete_secret=call)
    forbidden = HttpResponseError('Forbidden SECRET')
    forbidden.status_code = 403
    for exc, message in [(ClientAuthenticationError('no identity SECRET'), 'refused the platform identity'),
                         (forbidden, 'refused the platform identity'),
                         (ServiceRequestError('fdp.vault.azure.net unreachable SECRET'), 'Cannot reach')]:
        store = azure(failing(exc))
        with pytest.raises(SecretBackendError) as error:
            store.read('x')
        assert message in str(error.value) and 'SECRET' not in str(error.value)


# --- Google Secret Manager (fake client) ----------------------------------------------------------

class FakeSecretManager:
    def __init__(self):
        self.secrets = {}   # name -> {'replication':..., 'versions': [(name, data, state)]}

    def add_secret_version(self, request):
        from google.api_core import exceptions
        secret = self.secrets.get(request['parent'])
        if secret is None:
            raise exceptions.NotFound('secret not found')
        name = f"{request['parent']}/versions/{len(secret['versions']) + 1}"
        secret['versions'].append({'name': name, 'data': request['payload']['data'], 'state': 'ENABLED'})
        return SimpleNamespace(name=name)

    def create_secret(self, request):
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        assert re.fullmatch(r'[A-Za-z0-9_-]{1,255}', request['secret_id'])
        self.secrets[name] = {'replication': request['secret']['replication'], 'versions': []}

    def list_secret_versions(self, request):
        assert request['filter'] == 'state:ENABLED'
        return [SimpleNamespace(name=v['name']) for v in self.secrets[request['parent']]['versions']
                if v['state'] == 'ENABLED']

    def destroy_secret_version(self, request):
        for secret in self.secrets.values():
            for version in secret['versions']:
                if version['name'] == request['name']:
                    version.update(state='DESTROYED', data=None)

    def access_secret_version(self, request):
        from google.api_core import exceptions
        secret = self.secrets.get(request['name'].rsplit('/versions/', 1)[0])
        if not secret or not secret['versions']:
            raise exceptions.NotFound('not found')
        latest = secret['versions'][-1]
        if latest['state'] != 'ENABLED':
            raise exceptions.FailedPrecondition('destroyed')
        return SimpleNamespace(payload=SimpleNamespace(data=latest['data']))

    def delete_secret(self, request):
        from google.api_core import exceptions
        if request['name'] not in self.secrets:
            raise exceptions.NotFound('not found')
        del self.secrets[request['name']]


def gcp(client, **kw):
    return GcpSecretStore(Settings(secrets_backend='gcp', gcp_secrets_project='fdp-prod', **kw), client=client)


def test_gcp_round_trip_keeps_only_the_latest_version():
    pytest.importorskip('google.api_core')
    fake = FakeSecretManager()
    store = gcp(fake, gcp_secrets_locations='europe-west4, europe-west1')
    for path in PATHS:
        store.put(path, {'password': 'one'})
        store.put(path, {'password': 'two'})
        assert store.read(path) == {'password': 'two'}
    secret = fake.secrets[store.secret(PATHS[0])]
    assert [v['state'] for v in secret['versions']] == ['DESTROYED', 'ENABLED']
    assert secret['replication'] == {'user_managed': {'replicas': [{'location': 'europe-west4'}, {'location': 'europe-west1'}]}}
    store.delete(PATHS[0])
    assert store.read(PATHS[0]) == {}
    store.delete(PATHS[0])


def test_gcp_errors_are_sanitized():
    from google.api_core import exceptions

    def failing(exc):
        def call(**_):
            raise exc
        return SimpleNamespace(access_secret_version=call, add_secret_version=call, delete_secret=call)
    for exc, message in [(exceptions.PermissionDenied('projects/fdp-prod SECRET'), 'refused the platform identity'),
                         (exceptions.ServiceUnavailable('SECRET'), 'Cannot reach'),
                         (exceptions.InternalServerError('SECRET'), 'rejected')]:
        store = gcp(failing(exc))
        with pytest.raises(SecretBackendError) as error:
            store.read('x')
        assert message in str(error.value) and 'SECRET' not in str(error.value) and 'fdp-prod' not in str(error.value)


def test_backends_are_selected_by_settings_and_require_their_location(monkeypatch):
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'eu-west-1')
    assert isinstance(make_secret_store(Settings(secrets_backend='aws')), AwsSecretStore)
    with pytest.raises(ValueError):
        make_secret_store(Settings(secrets_backend='azure'))
    with pytest.raises(ValueError):
        make_secret_store(Settings(secrets_backend='gcp'))


def test_dynamic_database_credentials_need_a_vault_api_backend():
    store = gcp(FakeSecretManager())
    with pytest.raises(SecretBackendError, match='OpenBao or Vault'):
        store.credentials('sales', {'dynamic_role': 'readonly'})
