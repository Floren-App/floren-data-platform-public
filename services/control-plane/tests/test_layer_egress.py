"""Data network grants cannot widen into another layer or a platform service."""
from dataclasses import replace
from ipaddress import ip_address, ip_network

import pytest
from sqlalchemy import select

from app import layer_controller as controller, layer_egress as egress, layers, executions
from app.config import get_settings
from app.db import SessionLocal
from app.models import DataSource
from .conftest import as_
from .rbac_helpers import integration
from .test_layer_controller import CONFIG, MemoryKubernetes, due, operation, queue

BOUNDARY = egress.NetworkBoundary(('10.42.0.0/16', 'fd42::/64'), ('10.43.0.0/16', 'fd43::/112'),
                                 ('192.168.1.0/24', 'fd44::/64'), ('172.20.5.10/32',))
NETWORK_CONFIG = replace(CONFIG, pod_cidrs=BOUNDARY.pod_cidrs,
                         service_cidrs=BOUNDARY.service_cidrs, node_cidrs=BOUNDARY.node_cidrs,
                         protected_cidrs=BOUNDARY.protected_cidrs)


def source(kind='s3', egress_cidrs=('0.0.0.0/0', '::/0'), **config):
    return DataSource(id='abcdef012345', name='files', kind=kind,
                      config={'layer_id': layers.NON_PROD, **config}, egress=list(egress_cidrs))


def allowed(block, address):
    address = ip_address(address)
    return address in ip_network(block['cidr']) and not any(address in ip_network(excluded) for excluded in block.get('except', []))


def test_broad_grants_exclude_all_cluster_ranges_nodes_and_instance_metadata():
    documents, problems = egress.plan([source()], layers.NON_PROD, CONFIG, BOUNDARY)
    assert not problems
    peers = documents[0]['spec']['egress'][0]['to']
    ipv4, ipv6 = [peer['ipBlock'] for peer in peers]
    for address in ('10.42.5.8', '10.43.0.1', '192.168.1.10', '172.20.5.10', '169.254.169.254', '127.0.0.1'):
        assert not allowed(ipv4, address)
    for address in ('fd42::123', 'fd43::a', 'fd44::a', 'fe80::abcd', '::1', '::ffff:10.42.5.8'):
        assert not allowed(ipv6, address)
    assert allowed(ipv4, '203.0.113.9')
    assert allowed(ipv6, '2001:db8::1')
    assert documents[0]['spec']['podSelector']['matchLabels'] == {
        'fdp.floren.app/layer': layers.NON_PROD, 'fdp.floren.app/managed': 'true',
        egress.source_label('abcdef012345'): 'true'}
    assert documents[0]['spec']['egress'][0]['ports'] == [{'protocol': 'TCP', 'port': 443}]


@pytest.mark.parametrize('cidrs', [('10.42.5.8/32',), ('10.43.0.0/24',), ('169.254.169.254/32',),
                                  ('fd42::a/128',), ('203.0.113.1/24',), ('not-a-cidr',)])
def test_invalid_or_protected_targets_produce_denying_policy(cidrs):
    documents, problems = egress.plan([source(egress_cidrs=cidrs)], layers.NON_PROD, CONFIG, BOUNDARY)
    assert problems == ['DataEgressRejected']
    assert documents[0]['spec']['egress'] == []


def test_missing_cluster_boundary_or_address_family_is_closed():
    documents, problems = egress.plan([source()], layers.NON_PROD, CONFIG, egress.NetworkBoundary())
    assert problems == ['NetworkBoundaryMissing'] and documents[0]['spec']['egress'] == []
    ipv4_only = egress.NetworkBoundary(('10.42.0.0/16',), ('10.43.0.0/16',), ('192.168.1.0/24',))
    documents, problems = egress.plan([source()], layers.NON_PROD, CONFIG, ipv4_only)
    assert problems == ['DataEgressRejected'] and documents[0]['spec']['egress'] == []


@pytest.mark.parametrize('kind,config,ports', [
    ('postgres', {'port': 6432}, [6432]),
    ('iceberg', {'catalog_uri': 'http://catalog.example:8181', 'endpoint_url': 'https://s3.example'}, [443, 8181]),
    ('s3', {'endpoint_url': 'http://files.example:9000', 'sts_endpoint_url': 'https://sts.example:9443'}, [9000]),
])
def test_source_kind_controls_pods_and_ports(kind, config, ports):
    documents, problems = egress.plan([source(kind, **config)], layers.NON_PROD, CONFIG, BOUNDARY)
    assert not problems
    spec = documents[0]['spec']
    assert [port['port'] for port in spec['egress'][0]['ports']] == ports
    if kind != 's3':
        assert spec['podSelector']['matchLabels'] == {'fdp.floren.app/layer': layers.NON_PROD,
                                                     'app.kubernetes.io/component': 'trino'}


def test_controller_tracks_source_changes_and_disables_removed_grants(client):
    operation_id = queue(client)
    request = integration('files-test', 's3', layer_id=layers.NON_PROD, endpoint_url='https://s3.example')
    request['egress'] = ['203.0.113.0/24']
    created = client.put('/v1/admin/datasources/files-test', headers=as_('admin'), json=request)
    assert created.status_code == 200, created.text
    source_id = created.json()['id']
    # Managed sources never cause an allow in the shared namespace.
    from app.deps import get_orchestrator
    assert 'files-test' not in get_orchestrator().egress
    api = MemoryKubernetes()
    resources = controller.KubernetesResources(api)
    controller.reconcile(controller.claim_operation(), resources, NETWORK_CONFIG)
    policy_path = next(path for path, value in api.objects.items() if value['metadata']['name'] == 'data-source-' + source_id)
    assert api.objects[policy_path]['spec']['egress'][0]['to'] == [{'ipBlock': {'cidr': '203.0.113.0/24'}}]
    api.rollouts_ready()
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, NETWORK_CONFIG)
    assert operation(operation_id).phase == 'Ready'
    # Moving the target onto the cluster network clears the old allow before failure.
    request['egress'] = ['10.42.3.4/32']
    assert client.put('/v1/admin/datasources/files-test', headers=as_('admin'), json=request).status_code == 200
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, NETWORK_CONFIG)
    assert operation(operation_id).reason == 'DataEgressRejected'
    assert api.objects[policy_path]['spec']['egress'] == []
    # Retiring the source also clears any previous allow without deleting policies/PVCs.
    assert client.delete('/v1/admin/datasources/files-test', headers=as_('admin')).status_code == 204
    retry = client.post('/v1/admin/layers/' + layers.NON_PROD + '/infrastructure/retry',
                        headers=as_('admin'), json={'expected_generation': 1})
    assert retry.status_code == 202, retry.text
    controller.reconcile(controller.claim_operation(), resources, NETWORK_CONFIG)
    assert api.objects[policy_path]['spec']['egress'] == []
    assert all(method != 'DELETE' for method, _, _ in api.calls)


def test_missing_boundary_clears_previous_allow_before_reporting_failure(client):
    operation_id = queue(client)
    request = integration('missing-boundary', 's3', layer_id=layers.NON_PROD)
    request['egress'] = ['203.0.113.0/24']
    created = client.put('/v1/admin/datasources/missing-boundary', headers=as_('admin'), json=request)
    assert created.status_code == 200, created.text
    api = MemoryKubernetes()
    resources = controller.KubernetesResources(api)
    controller.reconcile(controller.claim_operation(), resources, NETWORK_CONFIG)
    policy_path = next(path for path, value in api.objects.items() if value['metadata']['name'] == 'data-source-' + created.json()['id'])
    assert api.objects[policy_path]['spec']['egress']
    due(operation_id)
    controller.reconcile(controller.claim_operation(), resources, CONFIG)
    assert operation(operation_id).reason == 'NetworkBoundaryMissing'
    assert api.objects[policy_path]['spec']['egress'] == []


def test_inventory_cannot_import_another_layers_sources(client):
    with SessionLocal() as db:
        test = source(egress_cidrs=('203.0.113.1/32',))
        prod = DataSource(id='abcdef012346', name='files-prod', kind='s3',
                          config={'layer_id': layers.PROD}, egress=['203.0.113.2/32'])
        db.add_all([test, prod]); db.commit()
        assert [row.id for row in egress.inventory(db, layers.NON_PROD)] == [test.id]
        assert [row.id for row in egress.inventory(db, layers.PROD)] == [prod.id]


def test_workload_source_labels_are_grant_scoped_and_sql_is_trino_only(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'query_executor', 'trino')
    sql_source = source('postgres')
    files = source()
    labels = executions._labels('ana', [files], 'compute')
    assert labels[egress.source_label(files.id)] == 'true'
    assert egress.source_label(files.id) not in executions._labels('ana', [], 'compute')
    assert egress.source_label(sql_source.id) not in executions._labels('ana', [sql_source], 'compute')


def test_source_api_rejects_bad_cidrs_before_saving(client):
    request = integration('bad-egress', 's3')
    request['egress'] = ['http://s3.example', '10.0.0.1/24']
    response = client.put('/v1/admin/datasources/bad-egress', headers=as_('admin'), json=request)
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(DataSource).where(DataSource.name == 'bad-egress')) is None

