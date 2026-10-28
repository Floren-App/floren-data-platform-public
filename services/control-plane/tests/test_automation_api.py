"""Endpoints infrastructure-as-code needs to read back, import and remove what it declares."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ComputePool, Run
from .conftest import as_
from .rbac_helpers import grant_group, integration
from .test_deploying import repo  # noqa: F401


def test_schema_can_be_read_back_and_deregistered_without_touching_data(client):
    client.put('/v1/admin/datasources/finance', headers=as_('admin'), json=integration('finance', 'postgres')).raise_for_status()
    schema = client.post('/v1/rbac/sources/finance/schemas', headers=as_('admin'), json={'name': 'reporting'}).json()
    grant_group(client, schema['id'], 'Readers', 'Reader', users=('ana',))
    read = client.get(f"/v1/rbac/resources/{schema['id']}", headers=as_('admin'))
    assert read.status_code == 200 and read.json()['name'] == 'reporting' and read.json()['kind'] == 'schema'
    assert client.get(f"/v1/rbac/resources/{schema['id']}", headers=as_('bob')).status_code == 403
    assert client.delete(f"/v1/rbac/resources/{schema['id']}", headers=as_('bob')).status_code == 403
    source_id = read.json()['parent_id']
    assert client.delete(f'/v1/rbac/resources/{source_id}', headers=as_('admin')).status_code == 409  # only schemas
    assert client.delete(f"/v1/rbac/resources/{schema['id']}", headers=as_('admin')).status_code == 204
    assert client.get(f"/v1/rbac/resources/{schema['id']}", headers=as_('admin')).status_code == 404
    assert client.get('/v1/admin/datasources', headers=as_('admin')).status_code == 200  # the source remains


def test_idle_pool_can_be_deleted_but_one_with_history_is_kept(client):
    body = {'name': 'tf-pool', 'cpu': 1, 'memory_gb': 2}
    assert client.put('/v1/admin/pools/tf-pool', headers=as_('admin'), json=body).status_code == 200
    assert client.delete('/v1/admin/pools/tf-pool', headers=as_('bob')).status_code == 403
    assert client.delete('/v1/admin/pools/tf-pool', headers=as_('admin')).status_code == 204
    assert client.delete('/v1/admin/pools/tf-pool', headers=as_('admin')).status_code == 404
    assert client.put('/v1/admin/pools/tf-pool', headers=as_('admin'), json=body).status_code == 200
    with SessionLocal() as db:
        pool = db.scalar(select(ComputePool).where(ComputePool.name == 'tf-pool'))
        db.add(Run(owner='ana', pool_id=pool.id, kind='script', entrypoint='x.py', status='succeeded'))
        db.commit()
    assert client.delete('/v1/admin/pools/tf-pool', headers=as_('admin')).status_code == 409


def test_governance_can_target_a_new_object_before_anyone_searched_the_catalog(client):
    from app.catalog import asset_id
    client.put('/v1/admin/datasources/finance', headers=as_('admin'), json=integration('finance', 'postgres')).raise_for_status()
    schema = client.post('/v1/rbac/sources/finance/schemas', headers=as_('admin'), json={'name': 'reporting'}).json()
    target = asset_id(schema['id'])  # the ID automation derives from the object's resource ID
    body = {'description': 'Sales reporting', 'domain': 'Finance', 'classification': 'internal'}
    saved = client.put(f'/v1/catalog/assets/{target}/governance', headers=as_('admin'), json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['governance']['domain'] == 'Finance'
    assert client.put(f'/v1/catalog/assets/{"0" * 32}/governance', headers=as_('admin'), json=body).status_code == 404
    assert client.get(f'/v1/catalog/assets/{target}', headers=as_('bob')).status_code == 404  # still permission-checked


def test_folder_with_only_undeployed_programs_can_be_deleted_keeping_their_history(client, repo):
    from .test_deploying import deploy
    from app.models import Resource
    folder = client.post('/v1/rbac/folders', headers=as_('admin'), json={'name': 'Finance'}).json()
    pub = deploy(client, repo, user='admin', program='explore', slug='explore', folder_id=folder['id']).json()
    assert client.delete(f"/v1/rbac/folders/{folder['id']}", headers=as_('admin')).status_code == 409  # still live
    assert client.delete(f"/v1/workloads/{pub['id']}", headers=as_('admin')).status_code == 204
    assert client.delete(f"/v1/rbac/folders/{folder['id']}", headers=as_('admin')).status_code == 204
    with SessionLocal() as db:
        kept = db.get(Resource, pub['resource_id'])
        assert kept is not None and kept.parent_id == folder['parent_id']


def test_well_known_roots_survive_a_concurrent_first_creation(client, monkeypatch):
    """Two requests that both found the API root missing must not fail on the second insert."""
    from app import rbac
    from app.models import Resource
    with SessionLocal() as db:
        existing = db.get(Resource, 'apis')
        assert existing is not None  # created at startup
        real_get = db.get
        calls = {'n': 0}

        def stale_get(model, ident, *a, **kw):  # the first lookup sees "missing", as a racing request would
            calls['n'] += 1
            return None if calls['n'] == 1 and ident == 'apis' else real_get(model, ident, *a, **kw)
        db.expunge(existing)
        monkeypatch.setattr(db, 'get', stale_get)
        root = rbac.api_root(db)
        assert root.id == 'apis' and root.kind == 'api-root'
