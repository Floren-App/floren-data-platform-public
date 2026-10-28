"""A team's dedicated warehouse: only admins create it, only its groups use it, and it has no slot limit."""
from .conftest import as_
from .test_api import start_workspace
from .test_capacity import limit, run
from .test_deploying import deploy, repo  # noqa: F401

DEDICATED = {'name': 'data-eng', 'cpu': 2, 'memory_gb': 8, 'max_concurrent': 1, 'dedicated': True,
             'allowed_groups': ['finance']}


def create(client, **changes):
    return client.put('/v1/admin/pools/data-eng', headers=as_('admin'), json={**DEDICATED, **changes})


def test_only_admins_create_one_and_it_belongs_to_groups(client):
    assert create(client, allowed_groups=[]).status_code == 422
    assert client.put('/v1/admin/pools/data-eng', headers=as_('ana'), json=DEDICATED).status_code == 403
    assert create(client).status_code == 200
    names = lambda user: {p['name'] for p in client.get('/v1/me', headers=as_(user)).json()['pools']}  # noqa: E731
    assert 'data-eng' in names('ana') and 'data-eng' not in names('bob')


def test_its_work_never_queues_and_does_not_count_elsewhere(client):
    create(client).raise_for_status()
    limit('small', 1)
    ws = start_workspace(client)
    runs = [client.post('/v1/runs', headers=as_('ana'), json={'workspace_id': ws, 'kind': 'script', 'entrypoint': 'x.py',
                                                               'pool': 'data-eng'}).json() for _ in range(3)]
    assert [r['status'] for r in runs] == ['running'] * 3  # max_concurrent does not apply
    assert run(client, ws)['status'] == 'running'  # the shared small warehouse is unaffected
    view = {w['name']: w for w in client.get('/v1/compute/warehouses', headers=as_('ana')).json()}['data-eng']
    assert (view['dedicated'], view['allowed_groups'], view['in_use'], view['queued']) == (True, ['finance'], 3, 0)


def test_only_its_groups_deploy_on_it(client, repo):  # noqa: F811
    create(client, allowed_groups=['marketing']).raise_for_status()
    refused = deploy(client, repo, program='sales-dashboard', slug='sales', title='Sales', warehouse='data-eng')
    assert refused.status_code == 404 and 'not available' in refused.text
    create(client).raise_for_status()  # now ana's group
    assert deploy(client, repo, program='sales-dashboard', slug='sales', title='Sales', warehouse='data-eng').json()['status'] == 'live'


def test_shared_uses_cannot_take_a_dedicated_warehouse(client):
    create(client).raise_for_status()
    settings = client.put('/v1/admin/settings/workspaces', headers=as_('admin'),
                          json={'warehouse': 'data-eng', 'max_running_per_user': 2})
    assert settings.status_code == 422 and 'dedicated' in settings.text
    layer = client.put('/v1/admin/layers/1a0000000002/infrastructure', headers=as_('admin'), json={
        'expected_generation': 0, 'spec': {'enabled': True, 'trino': {'workers': {'warehouse': 'data-eng'}}}})
    assert layer.status_code == 422 and 'dedicated' in layer.text
    client.put('/v1/admin/settings/workspaces', headers=as_('admin'),
               json={'warehouse': 'medium', 'max_running_per_user': 2}).raise_for_status()
    turned = client.put('/v1/admin/pools/medium', headers=as_('admin'), json={
        'name': 'medium', 'cpu': 4, 'memory_gb': 16, 'dedicated': True, 'allowed_groups': ['finance']})
    assert turned.status_code == 422 and 'workspaces use this warehouse' in turned.text


def test_existing_warehouses_are_shared_after_the_upgrade(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from app import main
    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE compute_pools (id VARCHAR(12) PRIMARY KEY, name VARCHAR(64), cpu FLOAT, "
                          "memory_gb FLOAT, gpu INTEGER, max_concurrent INTEGER, auto_suspend_seconds INTEGER)"))
        conn.execute(text("INSERT INTO compute_pools VALUES ('p1', 'small', 2, 8, 0, 6, 600)"))
    monkeypatch.setattr(main, "engine", old)
    main.add_missing_columns()
    with old.connect() as conn:
        assert conn.execute(text("SELECT dedicated FROM compute_pools")).scalar() in (0, False)
