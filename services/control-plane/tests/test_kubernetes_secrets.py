from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException

from app.config import Settings
from app.kubernetes_secrets import KubernetesSecretStore
from app.secrets import SecretBackendError


class Core:
    def __init__(self):
        self.rows = {}; self.calls=[]; self.conflict=False
    def read_namespaced_secret(self,name,namespace,**kwargs):
        self.calls.append(('get',namespace,name))
        if (namespace,name) not in self.rows:
            raise ApiException(status=404)
        return self.rows[namespace,name]
    def create_namespaced_secret(self,namespace,body,**kwargs):
        self.calls.append(('create',namespace,body.metadata.name))
        body.metadata.resource_version='1'
        self.rows[namespace,body.metadata.name]=body
        if self.conflict:
            self.conflict=False;raise ApiException(status=409)
    def replace_namespaced_secret(self,name,namespace,body,**kwargs):
        assert body.metadata.resource_version=='1'
        self.rows[namespace,name]=body
    def delete_namespaced_secret(self,name,namespace,**kwargs):
        if self.rows.pop((namespace,name),None) is None:
            raise ApiException(status=404)


def test_persistent_secret_and_certificate_roundtrip_scoped_to_configured_namespace():
    core=Core(); settings=Settings(k8s_secret_namespace='platform-secrets')
    store=KubernetesSecretStore(settings,core)
    value={'password':'p@ss:/秘密','certificate':'-----BEGIN CERTIFICATE-----\npublic\n',
           'private_key':'-----BEGIN PRIVATE KEY-----\nprivate\n'}
    assert store.read('env/sa/finance/tls')=={}
    store.put('env/sa/finance/tls',value)
    assert KubernetesSecretStore(settings,core).read('env/sa/finance/tls')==value
    assert all(call[1]=='platform-secrets' for call in core.calls)
    assert all('finance' not in call[2] for call in core.calls)
    store.put('env/sa/finance/tls',{'password':'changed'})
    assert store.read('env/sa/finance/tls')=={'password':'changed'}
    store.delete('env/sa/finance/tls');store.delete('env/sa/finance/tls')
    assert store.read('env/sa/finance/tls')=={}


def test_create_conflict_retries_and_backend_errors_never_expose_provider_response():
    core=Core();core.conflict=True;store=KubernetesSecretStore(Settings(),core)
    store.put('datasources/source',{'password':'private'})
    assert store.credentials('source',{})=={'password':'private'}
    def forbidden(*a,**kw):
        raise ApiException(status=403,reason='sensitive response body')
    core.read_namespaced_secret=forbidden
    with pytest.raises(SecretBackendError) as error:
        store.read('datasources/source')
    assert 'sensitive' not in str(error.value) and 'permissions' in str(error.value)


def test_invalid_payloads_and_dynamic_credentials_fail_explicitly():
    core=Core();store=KubernetesSecretStore(Settings(),core)
    core.rows[store.namespace,store.name('bad')]=SimpleNamespace(data={'payload':'not-base64'})
    with pytest.raises(SecretBackendError):store.read('bad')
    with pytest.raises(SecretBackendError):store.put('bad',{'value':123})
    with pytest.raises(SecretBackendError,match='OpenBao'):store.credentials('source',{'dynamic_role':'dynamic'})
    with pytest.raises(SecretBackendError,match='limit'):store.put('large',{'value':'x'*800000})


def test_api_reports_secret_backend_failure_without_persisting_metadata_or_leaking_payload(client):
    from app.deps import get_secret_store
    from .conftest import as_
    core=Core()
    def forbidden(*a,**kw):raise ApiException(status=403,reason='provider-private-data')
    core.read_namespaced_secret=forbidden
    client.app.dependency_overrides[get_secret_store]=lambda:KubernetesSecretStore(Settings(),core)
    response=client.put('/v1/secrets/user/ana/demo',headers=as_('ana'),json={'value':'private-secret'})
    assert response.status_code==503,response.text
    assert 'provider-private-data' not in response.text and 'private-secret' not in response.text
    assert client.get('/v1/secrets',headers=as_('ana')).json()==[]
