"""Retiring a layer scales it to zero and keeps its data; deleting data stays an explicit operator step."""
from datetime import timedelta

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from app import layer_controller as controller, layer_infrastructure as infra
from app.db import SessionLocal
from app.models import LayerOperation, Workspace, utcnow
from .conftest import as_
from .test_layer_controller import CONFIG, MemoryKubernetes
from .test_layer_infrastructure import managed


def operation(layer_id):
    with SessionLocal() as db:
        return db.scalars(select(LayerOperation).where(LayerOperation.layer_id == layer_id)
                          .order_by(LayerOperation.generation.desc())).first()


def run_controller(api):
    with SessionLocal() as db:  # due now
        for row in db.scalars(select(LayerOperation)):
            row.next_attempt_at, row.lease_expires_at = utcnow() - timedelta(seconds=1), None
        db.commit()
    claim = controller.claim_operation()
    assert claim is not None
    controller.reconcile(claim, controller.KubernetesResources(api), CONFIG)


@pytest.fixture
def ready_layer(client):
    layer = client.post('/v1/admin/layers', headers=as_('admin'), json={'name': 'Sandbox'}).json()
    path = f"/v1/admin/layers/{layer['id']}/infrastructure"
    assert client.put(path, headers=as_('admin'), json={'expected_generation': 0, 'spec': managed()}).status_code == 202
    api = MemoryKubernetes()
    run_controller(api)
    api.rollouts_ready()
    run_controller(api)
    assert operation(layer['id']).phase == 'Ready'
    return layer, path, api


def deployments(api):
    return {o['metadata']['name']: o['spec']['replicas'] for o in api.objects.values() if o['kind'] == 'Deployment'}


def test_retire_scales_to_zero_keeps_storage_and_refuses_new_work(client, ready_layer):
    layer, path, api = ready_layer
    retired = client.post(path + '/retire', headers=as_('admin'), json={'expected_generation': 1})
    assert retired.status_code == 202, retired.text
    run_controller(api)
    state = operation(layer['id'])
    assert (state.phase, state.reason) == ('Retired', 'StorageRetained')
    assert set(deployments(api).values()) == {0}
    namespace = infra.namespace_for(layer['id'], CONFIG.namespace_prefix)
    assert any(o['kind'] == 'PersistentVolumeClaim' for o in api.objects.values())  # retained, never deleted
    with SessionLocal() as db, pytest.raises(HTTPException) as error:
        infra.require_ready(db, layer['id'], 'workspace')
    assert error.value.detail['code'] == 'LayerRetired'
    refused = client.delete(f"/v1/admin/layers/{layer['id']}", headers=as_('admin'))
    assert refused.status_code == 409 and namespace in refused.json()['detail']

    # The operator deletes the namespace (and with it the data); the controller notices and never recreates it.
    for key in [k for k in api.objects if namespace in k]:
        del api.objects[key]
    run_controller(api)
    assert operation(layer['id']).reason == 'StorageDeleted'
    assert not any(namespace in key for key in api.objects)
    assert client.delete(f"/v1/admin/layers/{layer['id']}", headers=as_('admin')).status_code == 204


def test_retirement_waits_for_running_work_and_reactivation_restores_compute(client, ready_layer):
    layer, path, api = ready_layer
    with SessionLocal() as db:
        db.add(Workspace(name='busy', owner='admin', editor='vscode', status='running', environment_id=layer['id']))
        db.commit()
    blocked = client.post(path + '/retire', headers=as_('admin'), json={'expected_generation': 1})
    assert blocked.status_code == 409 and 'running workspaces' in blocked.json()['detail']
    with SessionLocal() as db:
        db.scalar(select(Workspace).where(Workspace.name == 'busy')).status = 'stopped'
        db.commit()
    assert client.post(path + '/retire', headers=as_('admin'), json={'expected_generation': 1}).status_code == 202
    run_controller(api)
    assert set(deployments(api).values()) == {0}
    spec = {**managed(), 'retired': False}
    assert client.put(path, headers=as_('admin'), json={'expected_generation': 2, 'spec': spec}).status_code == 202
    run_controller(api)
    api.rollouts_ready()
    run_controller(api)
    assert operation(layer['id']).phase == 'Ready'
    assert deployments(api)['fdp-trino-coordinator'] == 1


def test_only_managed_layers_can_be_retired(client):
    layer = client.post('/v1/admin/layers', headers=as_('admin'), json={'name': 'Plain'}).json()
    response = client.post(f"/v1/admin/layers/{layer['id']}/infrastructure/retire", headers=as_('admin'), json={'expected_generation': 1})
    assert response.status_code == 409
    assert client.post(f"/v1/admin/layers/{layer['id']}/infrastructure/retire", headers=as_('bob'), json={'expected_generation': 1}).status_code == 403


def test_retiring_through_a_full_update_has_the_same_precondition(client, ready_layer):
    layer, path, _ = ready_layer
    with SessionLocal() as db:
        db.add(Workspace(name='busy2', owner='admin', editor='vscode', status='running', environment_id=layer['id']))
        db.commit()
    spec = {**managed(), 'retired': True}
    blocked = client.put(path, headers=as_('admin'), json={'expected_generation': 1, 'spec': spec})
    assert blocked.status_code == 409 and 'running workspaces' in blocked.json()['detail']
