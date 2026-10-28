"""Trino workers are sized by a warehouse per layer, scale between a minimum and maximum, and take its slots."""
from app import capacity, layers
from app import layer_controller as controller
from app.db import SessionLocal
from app.layer_infrastructure import InfrastructureSpec
from app.layer_manifests import WorkerPlan, manifests
from .conftest import as_
from .test_layer_controller import CONFIG

PATH = '/v1/admin/layers/' + layers.NON_PROD + '/infrastructure'


def spec(**workers):
    return {'enabled': True, 'trino': {'coordinator': {'cpu': 0.5, 'memory': '1Gi'},
                                       'workers': {'warehouse': 'small', **workers}}}


def deployment(docs, name):
    return next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == name)


def test_workers_get_their_warehouse_size_and_the_coordinator_its_own():
    docs = manifests(layers.NON_PROD, 1, InfrastructureSpec.model_validate(spec(min=2, max=4)), CONFIG,
                     workers=WorkerPlan('small', 2, 8, 3))
    worker, coordinator = deployment(docs, 'fdp-trino-worker'), deployment(docs, 'fdp-trino-coordinator')
    assert worker['spec']['replicas'] == 3
    pod = worker['spec']['template']
    assert pod['spec']['containers'][0]['resources']['limits'] == {'cpu': '2000m', 'memory': '8192Mi'}
    assert pod['metadata']['labels']['fdp.floren.app/pool'] == 'small'
    assert coordinator['spec']['template']['spec']['containers'][0]['resources']['limits'] == {'cpu': '500m', 'memory': '1024Mi'}
    configs = {d['metadata']['name']: d['data'] for d in docs if d['kind'] == 'ConfigMap'}
    assert '-Xmx5734M' in configs['fdp-trino-worker']['jvm.config']  # 70% of 8 GB
    assert '-Xmx716M' in configs['fdp-trino-coordinator']['jvm.config']
    assert 'node-scheduler.include-coordinator=false' in configs['fdp-trino-coordinator']['config.properties']
    quota = next(d for d in docs if d['kind'] == 'ResourceQuota')
    assert quota  # sized for the coordinator plus the maximum of 4 workers (checked before any change)


def test_a_layer_needs_an_existing_worker_warehouse_large_enough_for_trino(client):
    def put(body):
        return client.put(PATH, headers=as_('admin'), json={'expected_generation': 0, 'spec': body})
    assert put({'enabled': True}).status_code == 422
    assert 'existing warehouse' in put(spec(warehouse='nope')).text
    client.put('/v1/admin/pools/tiny', headers=as_('admin'), json={'name': 'tiny', 'cpu': 0.5, 'memory_gb': 0.5})
    assert 'at least 1.0 GB' in put(spec(warehouse='tiny')).text
    assert put(spec(min=3, max=2)).status_code == 422
    assert put(spec(min=1, max=3)).status_code == 202


def test_earlier_specs_keep_their_sizes_as_the_coordinator():
    old = InfrastructureSpec.model_validate({'enabled': True, 'trino': {'cpu': 2, 'memory': '4Gi', 'heap': '3G', 'workers': 0}})
    assert (old.trino.coordinator.cpu, old.trino.coordinator.memory) == (2, '4Gi')
    assert (old.trino.workers.warehouse, old.trino.workers.min, old.trino.workers.max) == (None, 1, 1)


def test_running_workers_take_slots_and_their_warehouse_is_protected(client):
    assert client.put(PATH, headers=as_('admin'), json={'expected_generation': 0, 'spec': spec(min=2, max=5)}).status_code == 202
    with SessionLocal() as db:
        assert capacity.held(db)['small'] == 2  # the minimum is always granted
    small = next(p for p in client.get('/v1/admin/pools', headers=as_('admin')).json() if p['name'] == 'small')
    assert small['in_use'] == 2
    assert client.delete('/v1/admin/pools/small', headers=as_('admin')).status_code == 409
    shrink = client.put('/v1/admin/pools/small', headers=as_('admin'), json={'name': 'small', 'cpu': 2, 'memory_gb': 0.5})
    assert shrink.status_code == 422 and 'Trino workers' in shrink.text
    view = {w['name']: w for w in client.get('/v1/compute/warehouses', headers=as_('ana')).json()}['small']
    trino = [o for o in view['occupants'] if o['category'] == 'trino']
    assert [(o['name'], o['slots'], o['layer_name']) for o in trino] == [('Trino workers', 2, 'Non-Prod')]


def test_the_controller_resolves_the_warehouse_when_it_claims_the_layer(client):
    client.put(PATH, headers=as_('admin'), json={'expected_generation': 0, 'spec': spec(min=1, max=2)}).raise_for_status()
    claim = controller.claim_operation()
    assert claim.workers == WorkerPlan('small', 2, 8, 1)


def ready(client, **workers):
    """A Non-Prod layer whose infrastructure the controller reported Ready."""
    from app.models import LayerOperation
    response = client.put(PATH, headers=as_('admin'), json={'expected_generation': 0, 'spec': spec(**workers)})
    response.raise_for_status()
    with SessionLocal() as db:
        db.get(LayerOperation, response.json()['operation']['id']).phase = 'Ready'
        db.commit()


def workers_now():
    from app.models import Resource
    with SessionLocal() as db:
        config = db.get(Resource, layers.NON_PROD).config
        return config.get('trino_workers'), config.get('trino_scale') or {}


def test_load_adds_workers_at_once_and_removes_them_one_at_a_time_after_a_quiet_period(client):
    from datetime import timedelta
    from app.models import utcnow
    from app.trino_scaler import scale_once
    ready(client, min=1, max=4, queries_per_worker=2, scale_down_after_seconds=300)
    load = {'now': (7, 2)}
    start = utcnow()
    with SessionLocal() as db:
        assert scale_once(db, lambda layer, tls: load['now'], start) == [(layers.NON_PROD, 1, 4)]  # ceil(9 / 2) = 5, max 4
    assert workers_now()[0] == 4
    load['now'] = (1, 0)
    with SessionLocal() as db:
        assert scale_once(db, lambda layer, tls: load['now'], start + timedelta(seconds=10)) == []  # quiet from now on
        assert scale_once(db, lambda layer, tls: load['now'], start + timedelta(seconds=200)) == []
        assert scale_once(db, lambda layer, tls: load['now'], start + timedelta(seconds=311)) == [(layers.NON_PROD, 4, 3)]
        assert scale_once(db, lambda layer, tls: load['now'], start + timedelta(seconds=320)) == []  # waits again
        assert scale_once(db, lambda layer, tls: load['now'], start + timedelta(seconds=612)) == [(layers.NON_PROD, 3, 2)]
    load['now'] = (3, 0)  # load back up: the quiet period starts over
    with SessionLocal() as db:
        assert scale_once(db, lambda layer, tls: load['now'], start + timedelta(seconds=620)) == []
        assert workers_now()[1]['low_since'] is None


def test_extra_workers_only_start_with_free_slots_but_the_minimum_always_runs(client):
    from app.trino_scaler import scale_once
    client.put('/v1/admin/pools/small', headers=as_('admin'),
               json={'name': 'small', 'cpu': 2, 'memory_gb': 8, 'max_concurrent': 3}).raise_for_status()
    ready(client, min=1, max=5)
    with SessionLocal() as db:
        scale_once(db, lambda layer, tls: (40, 10))
    count, state = workers_now()
    assert count == 3 and state['waiting_for_warehouse'] == 'small'
    with SessionLocal() as db:
        assert capacity.held(db)['small'] == 3


def test_unknown_load_keeps_the_workers_that_run(client):
    from app.trino_scaler import scale_once
    ready(client, min=2, max=4)

    def unavailable(layer, tls):
        raise OSError('coordinator restarting')
    with SessionLocal() as db:
        assert scale_once(db, unavailable) == []
    count, state = workers_now()
    assert count == 2 and state['error'] == 'load unavailable'


def test_only_the_monitor_may_read_system_information_and_nothing_else(client):
    def decide(user, operation):
        body = {'input': {'context': {'identity': {'user': user, 'extraCredentials': {}}}, 'action': {'operation': operation}}}
        return client.post(f'/internal/trino/layers/{layers.NON_PROD}/allow', json=body).json()['result']
    assert decide('fdp-monitor', 'ReadSystemInformation') is True
    assert decide('fdp-monitor', 'WriteSystemInformation') is False
    assert decide('fdp-monitor', 'ExecuteQuery') is False
    assert decide('1a2b3c4d5e6f', 'ReadSystemInformation') is False


def test_removed_workers_drain_before_they_stop():
    docs = manifests(layers.NON_PROD, 1, InfrastructureSpec.model_validate(spec(drain_seconds=900)), CONFIG,
                     workers=WorkerPlan('small', 2, 8, 1))
    worker = deployment(docs, 'fdp-trino-worker')['spec']['template']['spec']
    assert worker['terminationGracePeriodSeconds'] == 930
    assert worker['containers'][0]['lifecycle']['preStop']['exec']['command'] == ['bash', '/etc/trino/worker-drain.sh']
    config = next(d for d in docs if d['kind'] == 'ConfigMap' and d['metadata']['name'] == 'fdp-trino-worker')['data']
    assert 'shutdown.grace-period=15s' in config['config.properties']
    assert 'url=http://127.0.0.1:8080' in config['worker-drain.sh'] and 'InternalToken.java' in config
    coordinator = deployment(docs, 'fdp-trino-coordinator')['spec']['template']['spec']
    assert 'lifecycle' not in coordinator['containers'][0] and 'terminationGracePeriodSeconds' not in coordinator


def test_the_layer_shows_how_many_workers_run_and_why(client):
    from app.trino_scaler import scale_once
    ready(client, min=1, max=3, queries_per_worker=2)
    with SessionLocal() as db:
        scale_once(db, lambda layer, tls: (3, 1))
    shown = client.get(PATH, headers=as_('admin')).json()['trino_workers']
    assert (shown['warehouse'], shown['running'], shown['running_queries'], shown['queued_queries']) == ('small', 2, 3, 1)


def test_load_is_read_from_the_coordinators_openmetrics():
    from app.trino_scaler import parse_load
    text = ('# TYPE trino_execution_name_QueryManager_RunningQueries gauge\n'
            'trino_execution_name_QueryManager_RunningQueries 3.0\n'
            'trino_execution_name_QueryManager_QueuedQueries{type="x"} 2.0\n'
            'trino_execution_name_QueryManager_RunningQueriesTotal 99.0\n')
    assert parse_load(text) == (3, 2)


def test_a_coordinator_needs_at_least_1gi():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match='at least 1Gi'):
        InfrastructureSpec.model_validate({'enabled': True, 'trino': {'coordinator': {'memory': '768Mi'}}})
