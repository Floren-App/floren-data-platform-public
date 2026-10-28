import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import deps, layers, managed_files, storage_access, layer_egress
from app.config import get_settings
from app.db import SessionLocal
from app.layer_infrastructure import namespace_for
from app.models import DataSource, Resource, utcnow
from .conftest import as_
from .test_layer_controller import CONFIG


def configure(client):
    client.get('/v1/me',headers=as_('admin')).raise_for_status()
    with SessionLocal() as db:
        layer = db.get(Resource,layers.NON_PROD)
        layer.config = {**layer.config,'infrastructure':{'enabled':True,
            'postgres':{'enabled':True},
            'rustfs':{'enabled':True,'image':'rustfs/rustfs:1.0.2','buckets':['lake','pokemon-landing']},
            'iceberg':{'enabled':True,'bucket':'lake','catalog':'lake'}}}
        db.commit()
    return {'name':'pokemon-files','kind':'s3',
            'config':{'layer_id':layers.NON_PROD,'managed_rustfs':True,'bucket':'pokemon-landing','prefix':'pokemon/test'}}


def test_managed_source_derives_endpoints_and_refuses_warehouse_buckets_or_credentials(client):
    body = configure(client)
    response = client.put('/v1/admin/datasources/pokemon-files',headers=as_('admin'),json=body)
    assert response.status_code == 200,response.text
    config = response.json()['config']
    assert config['endpoint_url'] == 'http://fdp-rustfs.%s.svc:9000' % namespace_for(layers.NON_PROD,get_settings().layer_namespace_prefix or 'fdp-layer')
    assert config['sts_endpoint_url'] == config['endpoint_url']
    for override in ({'bucket':'lake'},{'bucket':'undeclared'},{'endpoint_url':'http://other-layer:9000'}):
        candidate = {**body,'config':{**body['config'],**override}}
        assert client.put('/v1/admin/datasources/pokemon-files',headers=as_('admin'),json=candidate).status_code == 422
    assert client.put('/v1/admin/datasources/pokemon-files',headers=as_('admin'),
                      json={**body,'provisioner_secret':{'access_key_id':'supplied','secret_access_key':'supplied'}}).status_code == 422


def test_only_scoped_sts_session_keys_leave_control_plane(client,monkeypatch):
    body = configure(client)
    client.put('/v1/admin/datasources/pokemon-files',headers=as_('admin'),json=body).raise_for_status()
    namespace = namespace_for(layers.NON_PROD,get_settings().layer_namespace_prefix or 'fdp-layer')
    orch = deps.get_orchestrator()
    orch.layer_secrets[(namespace,'fdp-rustfs-credentials')] = ('digest',{'access-key':'ROOT','secret-key':'ROOT-SECRET'})
    seen = {}
    class STS:
        def assume_role(self,**kwargs):
            seen['request'] = kwargs
            return {'Credentials':{'AccessKeyId':'SCOPED','SecretAccessKey':'SCOPED-SECRET','SessionToken':'SESSION',
                                   'Expiration':utcnow()+timedelta(minutes=15)}}
    def client_(service,**kwargs):
        assert service == 'sts'
        seen['connection'] = kwargs
        return STS()
    monkeypatch.setattr('boto3.client',client_)
    with SessionLocal() as db:
        source = db.scalar(select(DataSource).where(DataSource.name=='pokemon-files'))
        schema = Resource(name='landing',config={'prefix':'landing'})
        credentials, access = storage_access.credentials(source,{'landing':schema},{'landing':'Reader'},
                                                         deps.get_secret_store(),db=db)
    assert credentials['access_key_id'] == 'SCOPED' and 'ROOT' not in json.dumps(credentials)
    assert seen['connection']['aws_access_key_id'] == 'ROOT'
    policy = json.loads(seen['request']['Policy'])
    assert 's3:PutObject' not in json.dumps(policy)
    assert 'pokemon-landing/pokemon/test/landing/' in json.dumps(policy)
    assert access['schemas'] == {'landing':'Reader'}


def test_managed_network_paths_are_source_scoped_and_namespace_local():
    source = DataSource(id='abcdef012345',name='files',kind='s3',config={'managed_rustfs':True},egress=[])
    documents, problems = layer_egress.plan([source],layers.NON_PROD,CONFIG,layer_egress.NetworkBoundary())
    assert not problems and len(documents) == 2
    outgoing,incoming = [d['spec'] for d in documents]
    selected = outgoing['podSelector']['matchLabels']
    assert selected['fdp.floren.app/layer'] == layers.NON_PROD
    assert selected[layer_egress.source_label(source.id)] == 'true'
    assert outgoing['egress'][0]['to'] == [{'podSelector':{'matchLabels':{
        'fdp.floren.app/layer':layers.NON_PROD,'app.kubernetes.io/component':'rustfs'}}}]
    assert incoming['ingress'][0]['from'] == [{'podSelector':outgoing['podSelector']}]
    assert layer_egress.disabled(documents[1])['spec']['ingress'] == []


def test_browser_resolves_managed_storage_context_and_uses_namespace_reader_credentials(client, monkeypatch):
    from .rbac_helpers import grant_group

    body = configure(client)
    client.put('/v1/admin/datasources/pokemon-files', headers=as_('admin'), json=body).raise_for_status()
    schema = client.post('/v1/rbac/sources/pokemon-files/schemas', headers=as_('admin'),
                         json={'name': 'landing', 'prefix': 'landing'}).json()
    client.post(f"/v1/rbac/resources/{schema['id']}/provision", headers=as_('admin')).raise_for_status()
    grant_group(client, schema['id'], 'Landing readers', 'Reader', users=('ana',))
    namespace = namespace_for(layers.NON_PROD, get_settings().layer_namespace_prefix or 'fdp-layer')
    deps.get_orchestrator().layer_secrets[(namespace, 'fdp-rustfs-credentials')] = (
        'digest', {'access-key': 'ROOT', 'secret-key': 'ROOT-SECRET'})
    seen = []

    class STS:
        def assume_role(self, **kwargs):
            policy = json.loads(kwargs['Policy'])
            assert 's3:PutObject' not in json.dumps(policy)
            assert 'pokemon-landing/pokemon/test/landing/' in json.dumps(policy)
            return {'Credentials': {'AccessKeyId': 'SCOPED', 'SecretAccessKey': 'SCOPED-SECRET',
                                    'SessionToken': 'SESSION', 'Expiration': utcnow() + timedelta(minutes=15)}}

    class Storage:
        def list_objects_v2(self, **kwargs):
            seen.append(kwargs)
            return {'CommonPrefixes': [{'Prefix': 'pokemon/test/landing/batch-1/'}]}

    def client_(service, **kwargs):
        if service == 'sts':
            assert kwargs['aws_access_key_id'] == 'ROOT'
            return STS()
        assert service == 's3'
        assert kwargs['aws_access_key_id'] == 'SCOPED'
        assert kwargs['aws_session_token'] == 'SESSION'
        return Storage()

    monkeypatch.setattr('boto3.client', client_)
    url = f"/v1/data/schemas/{schema['id']}/children"
    response = client.get(url, headers=as_('ana'), params={'limit': 2})
    assert response.status_code == 200, response.text
    assert response.json() == {'children': [{'name': 'batch-1', 'kind': 'folder', 'path': 'batch-1'}],
                               'next_cursor': None}
    assert seen == [{'Bucket': 'pokemon-landing', 'Prefix': 'pokemon/test/landing/', 'Delimiter': '/', 'MaxKeys': 2}]
    assert 'ROOT' not in response.text and 'SCOPED' not in response.text
    assert client.get(url, headers=as_('bob')).status_code == 404
    assert len(seen) == 1
