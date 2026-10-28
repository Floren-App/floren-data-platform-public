"""Layer RustFS and the Iceberg catalog generated for the layer's own Trino."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app import layer_infrastructure as infra, layers
from app.layer_manifests import CATALOG_CONFIGMAP, ProvisioningError, manifests
from app.rustfs_buckets import ensure
from .conftest import as_
from .test_layer_controller import WORKERS, CONFIG, due, queue

NAMESPACE = 'fdp-layer-' + layers.NON_PROD


def lakehouse(**changes):
    spec = {'enabled': True, 'trino': {'workers': {'warehouse': 'trino'}},
            'postgres': {'enabled': True, 'credentials_secret': 'postgres-passwords', 'catalog': 'warehouse_np'},
            'rustfs': {'enabled': True, 'credentials_secret': 'rustfs-keys', 'image': 'rustfs/rustfs:1.0.2',
                       'buckets': ['lake', 'files']},
            'iceberg': {'enabled': True, 'catalog': 'lake_np', 'bucket': 'lake'}}
    for key, value in changes.items():
        spec[key] = {**spec.get(key, {}), **value}
    return spec


def docs(spec):
    return manifests(layers.NON_PROD, 1, infra.InfrastructureSpec.model_validate(spec), CONFIG, workers=WORKERS)


def named(documents, kind, name):
    return next(d for d in documents if d['kind'] == kind and d['metadata']['name'] == name)


def test_rustfs_keeps_its_volume_and_reads_credentials_only_from_files_in_its_layer():
    documents = docs(lakehouse())
    pvc = named(documents, 'PersistentVolumeClaim', 'fdp-rustfs')
    assert pvc['metadata']['annotations']['fdp.floren.app/retention'] == 'Retain'
    pod = named(documents, 'Deployment', 'fdp-rustfs')['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False and pod['securityContext']['runAsNonRoot'] is True
    server, buckets = pod['containers']
    env = {e['name']: e.get('value') for e in server['env']}
    assert env['RUSTFS_ACCESS_KEY_FILE'] == '/rustfs-auth/access-key' and env['RUSTFS_CONSOLE_ENABLE'] == 'false'
    assert 'RUSTFS_ACCESS_KEY' not in env and not any('valueFrom' in e for e in server['env'])
    auth = next(v['secret'] for v in pod['volumes'] if v['name'] == 'auth')
    assert auth['secretName'] == 'rustfs-keys' and auth['defaultMode'] == 0o440
    assert buckets['command'][-2:] == ['lake', 'files'] and buckets['image'] == CONFIG.artifact_image
    assert buckets['readinessProbe']  # the layer is not Ready until the buckets exist
    assert not any(d['kind'] == 'Secret' for d in documents)
    # Only the layer's Trino reaches the object store.
    ingress = named(documents, 'NetworkPolicy', 'rustfs-ingress')['spec']['ingress']
    assert ingress == [{'from': [{'podSelector': {'matchLabels': {'fdp.floren.app/layer': layers.NON_PROD,
        'app.kubernetes.io/component': 'trino'}}},
        {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': CONFIG.platform_namespace}},
         'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'fdp-control-plane'}}}],
        'ports': [{'protocol': 'TCP', 'port': 9000}]}]


def test_iceberg_catalog_uses_the_layer_database_and_bucket_with_credentials_from_secrets():
    documents = docs(lakehouse())
    catalogs = named(documents, 'ConfigMap', CATALOG_CONFIGMAP)['data']
    lake = catalogs['lake_np.properties']
    assert 'iceberg.catalog.type=jdbc' in lake
    assert f'connection-url=jdbc:postgresql://fdp-postgres.{NAMESPACE}.svc:5432/iceberg\n' in lake
    assert 'default-warehouse-dir=s3://lake/' in lake and f's3.endpoint=http://fdp-rustfs.{NAMESPACE}.svc:9000' in lake
    assert 'iceberg.security=SYSTEM' in lake and 'add-files-procedure.enabled=false' in lake
    assert '${ENV:FDP_RUSTFS_SECRET_KEY}' in lake and '${ENV:FDP_POSTGRES_ENGINE_PASSWORD}' in lake
    warehouse = catalogs['warehouse_np.properties']
    assert 'connector.name=postgresql' in warehouse and '5432/fdp\n' in warehouse
    coordinator = named(documents, 'Deployment', 'fdp-trino-coordinator')['spec']['template']['spec']
    env = {e['name']: e['valueFrom']['secretKeyRef'] for e in coordinator['containers'][0]['env'] if 'valueFrom' in e}
    assert env['FDP_POSTGRES_ENGINE_PASSWORD'] == {'name': 'postgres-passwords', 'key': 'engine-password'}
    assert env['FDP_RUSTFS_ACCESS_KEY'] == {'name': 'rustfs-keys', 'key': 'access-key'}
    volume = next(v for v in coordinator['volumes'] if v['name'] == 'catalogs')
    assert volume == {'name': 'catalogs', 'configMap': {'name': CATALOG_CONFIGMAP}} or \
        volume['projected']['sources'] == [{'configMap': {'name': CATALOG_CONFIGMAP}}]
    # Iceberg metadata lives in its own database, created for existing layers too.
    postgres = named(documents, 'Deployment', 'fdp-postgres')['spec']['template']['spec']
    setup = next(c for c in postgres['containers'] if c['name'] == 'setup')
    assert setup['command'][-1] == 'iceberg' and setup['readinessProbe']
    config = named(documents, 'ConfigMap', 'fdp-postgres')['data']
    assert 'CREATE TABLE IF NOT EXISTS iceberg_tables' in config['iceberg.sql']  # Trino does not create them


def test_controller_generates_storage_credentials_only_when_none_are_named():
    from app.layer_manifests import generated_credentials
    spec = infra.InfrastructureSpec.model_validate(lakehouse(postgres={'credentials_secret': None},
                                                             rustfs={'credentials_secret': None}))
    generated = dict(generated_credentials(spec))
    assert set(generated) == {'fdp-postgres-credentials', 'fdp-rustfs-credentials', 'fdp-trino-events'}
    assert set(generated['fdp-rustfs-credentials']()) == {'access-key', 'secret-key'}
    assert generated['fdp-postgres-credentials']() != generated['fdp-postgres-credentials']()
    rendered = docs(spec)
    assert 'fdp-rustfs-credentials' in str(named(rendered, 'Deployment', 'fdp-rustfs'))
    assert 'fdp-postgres-credentials' in str(named(rendered, 'Deployment', 'fdp-trino-coordinator'))
    assert not any(d['kind'] == 'Secret' for d in rendered)  # values are created by the controller, not applied
    # Named storage credentials: only the layer's query log token is generated.
    assert [name for name, _ in generated_credentials(infra.InfrastructureSpec.model_validate(lakehouse()))] == ['fdp-trino-events']


def test_operator_catalogs_and_generated_catalogs_are_mounted_together():
    spec = lakehouse(trino={'catalog_secret': 'extra-catalogs'})
    coordinator = named(docs(spec), 'Deployment', 'fdp-trino-coordinator')['spec']['template']['spec']
    volume = next(v for v in coordinator['volumes'] if v['name'] == 'catalogs')
    assert volume['projected']['sources'] == [{'secret': {'name': 'extra-catalogs'}}, {'configMap': {'name': CATALOG_CONFIGMAP}}]
    # Layers without generated catalogs keep their existing volume unchanged.
    plain = named(docs({'enabled': True, 'trino': {'catalog_secret': 'extra-catalogs'}}), 'Deployment',
                  'fdp-trino-coordinator')['spec']['template']['spec']
    assert {'name': 'catalogs', 'secret': {'secretName': 'extra-catalogs'}} in plain['volumes']


def test_catalog_changes_roll_trino():
    def config_hash(spec):
        return named(docs(spec), 'Deployment', 'fdp-trino-coordinator')['spec']['template']['metadata'][
            'annotations']['fdp.floren.app/config-hash']
    assert config_hash(lakehouse()) != config_hash(lakehouse(iceberg={'bucket': 'files'}))


def test_tls_database_catalogs_verify_the_server_with_the_layer_truststore():
    tls = {'mode': 'existing', 'secret_name': 'postgres-tls'}
    with pytest.raises(ValidationError, match='trust_bundle'):
        infra.InfrastructureSpec.model_validate(lakehouse(postgres={'tls': tls}))
    spec = lakehouse(postgres={'tls': tls}, trino={'tls': {'mode': 'existing', 'secret_name': 'trino-tls',
                                                          'trust_bundle': {'name': 'layer-ca', 'key': 'ca.pem'}}})
    lake = named(docs(spec), 'ConfigMap', CATALOG_CONFIGMAP)['data']['lake_np.properties']
    assert '5432/iceberg?sslmode=verify-full&sslfactory=org.postgresql.ssl.DefaultJavaSSLFactory' in lake


@pytest.mark.parametrize('changes,message', [
    ({'rustfs': {'enabled': False}}, 'needs the layer PostgreSQL'),
    ({'postgres': {'enabled': False, 'catalog': None}}, 'needs the layer PostgreSQL'),
    ({'iceberg': {'bucket': 'other'}}, 'one of the RustFS buckets'),
    ({'iceberg': {'catalog': None}}, 'catalog name'),
    ({'iceberg': {'catalog': 'warehouse_np'}}, 'different names'),
    ({'postgres': {'catalog': 'system'}}, 'reserved'),
    ({'rustfs': {'buckets': ['lake', 'lake']}}, 'distinct'),
    ({'rustfs': {'buckets': ['Bad_Name']}}, 'distinct'),
    ({'rustfs': {'tls': {'mode': 'existing', 'secret_name': 'rustfs-tls'}}}, 'needs trino.tls.trust_bundle'),
    ({'rustfs': {'image': 'rustfs/rustfs:latest'}}, 'explicit version'),
])
def test_incomplete_or_unsafe_storage_combinations_are_refused(changes, message):
    with pytest.raises(ValidationError, match=message):
        infra.InfrastructureSpec.model_validate(lakehouse(**changes))


def test_storage_must_fit_the_layer_budget():
    with pytest.raises(ProvisioningError) as error:
        docs(lakehouse(quota={'persistent_volume_claims': 2}))
    assert error.value.reason == 'InsufficientQuota'


def test_object_storage_updates_cannot_drop_storage_or_swap_its_credentials(client):
    spec = infra.InfrastructureSpec.model_validate(lakehouse()).model_dump(mode='json')
    operation_id = queue(client, spec)
    due(operation_id, phase='Ready')
    path = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'
    for changes in ({'enabled': False}, {'size': '1Gi'}, {'credentials_secret': 'other-keys'}):
        desired = deepcopy(spec)
        desired['rustfs'].update(changes)
        if changes.get('enabled') is False:
            desired['iceberg']['enabled'] = False
        response = client.put(path, headers=as_('admin'), json={'expected_generation': 1, 'spec': desired})
        assert response.status_code == 409, response.text
    grown = deepcopy(spec)
    grown['rustfs']['buckets'].append('exports')
    grown['rustfs']['image'] = 'rustfs/rustfs:1.0.3'
    assert client.put(path, headers=as_('admin'), json={'expected_generation': 1, 'spec': grown}).status_code == 202


def test_bucket_sidecar_only_creates_missing_buckets():
    class S3:
        def __init__(self):
            self.created = []

        def list_buckets(self):
            return {'Buckets': [{'Name': 'lake'}]}

        def create_bucket(self, Bucket):
            self.created.append(Bucket)
    s3 = S3()
    assert ensure(s3, ['lake', 'files']) == ['files'] and s3.created == ['files']


TLS_TRINO = {'tls': {'mode': 'existing', 'secret_name': 'trino-tls', 'trust_bundle': {'name': 'layer-ca', 'key': 'ca.pem'}}}


def test_rustfs_tls_serves_its_certificate_restarts_on_renewal_and_is_verified_by_clients():
    spec = lakehouse(trino=TLS_TRINO, rustfs={'tls': {'mode': 'cert_manager', 'secret_name': 'rustfs-tls', 'issuer': {'name': 'layer-ca'}}})
    documents = docs(spec)
    certificate = named(documents, 'Certificate', 'fdp-rustfs')
    assert certificate['spec']['dnsNames'] == [f'fdp-rustfs.{NAMESPACE}.svc']
    pod = named(documents, 'Deployment', 'fdp-rustfs')['spec']['template']['spec']
    containers = {c['name']: c for c in pod['containers']}
    assert {'name': 'RUSTFS_TLS_PATH', 'value': '/tls'} in containers['rustfs']['env']
    tls = next(v['secret'] for v in pod['volumes'] if v['name'] == 'tls')
    assert tls['secretName'] == 'rustfs-tls' and [i['path'] for i in tls['items']] == ['rustfs_cert.pem', 'rustfs_key.pem']
    assert containers['tls-restart']['command'][-4:] == ['--watch', '/tls', '--process', 'rustfs']
    assert pod['shareProcessNamespace'] is True and 'serviceAccountToken' not in str(containers['tls-restart'])
    assert pod['hostAliases'] == [{'ip': '127.0.0.1', 'hostnames': [f'fdp-rustfs.{NAMESPACE}.svc']}]
    assert f'https://fdp-rustfs.{NAMESPACE}.svc:9000' in containers['buckets']['command']
    assert next(v['secret'] for v in pod['volumes'] if v['name'] == 'trust')['secretName'] == 'layer-ca'
    lake = named(documents, 'ConfigMap', CATALOG_CONFIGMAP)['data']['lake_np.properties']
    assert f's3.endpoint=https://fdp-rustfs.{NAMESPACE}.svc:9000' in lake


def test_rustfs_tls_cannot_be_downgraded(client):
    spec = infra.InfrastructureSpec.model_validate(lakehouse(trino=TLS_TRINO, rustfs={
        'tls': {'mode': 'existing', 'secret_name': 'rustfs-tls'}})).model_dump(mode='json')
    operation_id = queue(client, spec)
    due(operation_id, phase='Ready')
    spec['rustfs']['tls'] = {'mode': 'disabled'}
    response = client.put('/v1/admin/layers/' + layers.NON_PROD + '/infrastructure', headers=as_('admin'),
                          json={'expected_generation': 1, 'spec': spec})
    assert response.status_code == 409 and 'downgraded' in response.text


def test_restart_sidecar_stops_only_the_named_process_when_the_certificate_changes(tmp_path):
    from app import tls_restart
    proc = tmp_path / 'proc'
    for pid, name in ((7, 'rustfs'), (8, 'python'), (9, 'rustfs')):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / 'comm').write_text(name + '\n')
    stopped = []
    assert tls_restart.restart('rustfs', str(proc), kill=lambda pid, sig: stopped.append(pid)) == 2
    assert sorted(stopped) == [7, 9]
    watched = tmp_path / 'tls'
    watched.mkdir()
    assert tls_restart.revision(str(watched)) == ''
    (watched / '..data').symlink_to('..2026_10_22_rev1')
    assert tls_restart.revision(str(watched)) == '..2026_10_22_rev1'
