"""Trino catalogs for external sources: generated per layer, from engine credentials only."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import deps, layers, source_catalogs as sc
from app.config import get_settings
from app.db import SessionLocal
from app.layer_infrastructure import InfrastructureSpec
from app.layer_manifests import manifests
from app.models import LayerOperation, Resource
from .conftest import as_
from .rbac_helpers import integration
from .test_layer_controller import WORKERS, CONFIG
from .test_layer_infrastructure import managed

NS = 'dc-x-layer-' + layers.NON_PROD
ENGINE = {'secret': {'username': 'trino_engine', 'password': 'engine-pass'}}


def pg(**config):
    return SimpleNamespace(kind='postgres', name='sales', config={'host': 'db.example', 'port': 5432, 'database': 'sales', **config})


def test_postgres_catalog_uses_the_engine_login_and_layer_trust_for_verified_tls():
    text = sc.properties(pg(sslmode='verify-full'), {'username': 'trino_engine', 'password': 'p'})
    assert 'connector.name=postgresql\n' in text
    assert 'connection-url=jdbc:postgresql://db.example:5432/sales?sslmode=verify-full&sslfactory=org.postgresql.ssl.DefaultJavaSSLFactory\n' in text
    assert 'connection-user=trino_engine\n' in text and 'connection-password=p\n' in text
    assert 'sslfactory' not in sc.properties(pg(sslmode='require'), {'username': 'u', 'password': 'p'})
    assert sc.properties(pg(), {}) is None  # no engine login: the operator configures the catalog


def test_iceberg_catalog_uses_the_rest_catalog_and_engine_storage_credentials():
    source = SimpleNamespace(kind='iceberg', name='lake', config={
        'catalog_uri': 'https://catalog.example', 'warehouse': 's3://tables/', 'endpoint_url': 'https://s3.example', 'region': 'eu-west-1'})
    text = sc.properties(source, {'access_key_id': 'AK', 'secret_access_key': 'SK'})
    for line in ('iceberg.catalog.type=rest', 'iceberg.rest-catalog.uri=https://catalog.example', 's3.endpoint=https://s3.example',
                 's3.path-style-access=true', 's3.aws-secret-key=SK', 'iceberg.add-files-procedure.enabled=false'):
        assert line + '\n' in text


def test_values_are_escaped_and_substitution_or_line_breaks_are_refused():
    text = sc.properties(pg(), {'username': 'u', 'password': ' back\\slash'})
    assert 'connection-password=\\ back\\\\slash\n' in text
    for bad in ('x${ENV:TRINO_INTERNAL_SECRET}', 'two\nlines'):
        with pytest.raises(sc.UnsafeCredential):
            sc.properties(pg(), {'username': 'u', 'password': bad})


@pytest.fixture
def ready_layer(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'layer_namespace_prefix', 'dc-x-layer')
    spec = {**managed(), 'postgres': {'enabled': True, 'catalog': 'np_warehouse'}}
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    assert client.put(path, headers=as_('admin'), json={'expected_generation': 0, 'spec': spec}).status_code == 202
    with SessionLocal() as db:
        db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layers.NON_PROD)).phase = 'Ready'
        db.commit()


def put(client, body):
    response = client.put(f"/v1/admin/datasources/{body['name']}", headers=as_('admin'), json=body)
    assert response.status_code == 200, response.text


def test_registering_sources_writes_only_that_layers_catalogs_with_engine_credentials(client, ready_layer):
    orch = deps.get_orchestrator()
    put(client, integration('sales', 'postgres', ENGINE, layer_id=layers.NON_PROD))
    put(client, integration('prod-sales', 'postgres', ENGINE, layer_id=layers.PROD))           # another layer
    put(client, integration('bound', 'postgres', ENGINE, layer_id=layers.NON_PROD, catalog='np_warehouse'))  # the layer's own DB
    put(client, integration('manual', 'postgres', layer_id=layers.NON_PROD))                     # no engine login
    digest, files = orch.layer_secrets[(NS, sc.SECRET)]
    assert set(files) == {'sales.properties'}
    assert 'connection-user=trino_engine' in files['sales.properties'] and 'governance' not in files['sales.properties']
    with SessionLocal() as db:
        layer = db.get(Resource, layers.NON_PROD)
        assert layer.config['source_catalogs'] == digest
        operation = db.scalar(select(LayerOperation).where(LayerOperation.layer_id == layers.NON_PROD))
        assert operation.next_attempt_at is not None  # the controller rolls Trino now
    # Removing the source removes its catalog; with none left the Secret goes too.
    assert client.delete('/v1/admin/datasources/sales', headers=as_('admin')).status_code == 204
    assert (NS, sc.SECRET) not in orch.layer_secrets
    with SessionLocal() as db:
        assert db.get(Resource, layers.NON_PROD).config['source_catalogs'] == ''


def test_catalogs_wait_for_the_layer_namespace(client, ready_layer):
    orch = deps.get_orchestrator()
    orch.missing_namespaces.add(NS)
    put(client, integration('sales', 'postgres', ENGINE, layer_id=layers.NON_PROD))
    with SessionLocal() as db:
        assert not db.get(Resource, layers.NON_PROD).config.get('source_catalogs')
    orch.missing_namespaces.clear()
    sc.reconcile_all(SessionLocal, orch, deps.get_secret_store())  # the background pass
    assert set(orch.layer_secrets[(NS, sc.SECRET)][1]) == {'sales.properties'}
    with SessionLocal() as db:
        assert db.get(Resource, layers.NON_PROD).config['source_catalogs']


def test_trino_mounts_source_catalogs_and_rolls_when_they_change():
    spec = InfrastructureSpec.model_validate(managed())

    def coordinator(digest):
        return next(d for d in manifests(layers.NON_PROD, 1, spec, CONFIG, digest, workers=WORKERS)
                    if d['kind'] == 'Deployment' and d['metadata']['name'] == 'fdp-trino-coordinator')['spec']['template']
    without, first, second = coordinator(''), coordinator('aaa'), coordinator('bbb')
    assert 'fdp.floren.app/source-catalogs' not in without['metadata']['annotations']
    assert not any('projected' in v for v in without['spec']['volumes'] if v['name'] == 'catalogs')  # unchanged for others
    volume = next(v for v in first['spec']['volumes'] if v['name'] == 'catalogs')
    assert {'secret': {'name': sc.SECRET, 'optional': True}} in volume['projected']['sources']
    assert first['metadata']['annotations']['fdp.floren.app/source-catalogs'] != second['metadata']['annotations']['fdp.floren.app/source-catalogs']
