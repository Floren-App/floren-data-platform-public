"""Readiness explains who acts and what will work in a workspace; it never authorizes."""
from app.auth import mint_token
from .conftest import as_
from .test_api import start_workspace


def checks(body):
    return {c['key']: c for c in body['checks']}


def test_readiness_names_the_acting_context_and_each_capability(client):
    ws = start_workspace(client)
    response = client.get(f'/v1/workspaces/{ws}/readiness', headers=as_('ana'))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['context']['kind'] == 'person' and body['context']['username'] == 'ana'
    assert body['context']['group_id'] and body['context']['layer_id']
    found = checks(body)
    assert set(found) == {'workspace', 'config', 'runs', 'warehouse', 'logs', 'outputs'}
    assert found['workspace']['state'] == 'ready'
    assert all(c['state'] in ('ready', 'missing', 'unavailable', 'unknown') for c in found.values())
    # The editor credential sees the same workspace as an editor, not as the person.
    editor = {'Authorization': 'Bearer ' + mint_token('execution', 'ana', 3600, ws=ws)}
    assert client.get(f'/v1/workspaces/{ws}/readiness', headers=editor).json()['context']['kind'] == 'editor'


def test_readiness_agrees_with_admission(client):
    """When starting runs is reported missing, starting one is refused, and the other way round."""
    ws = start_workspace(client)
    state = checks(client.get(f'/v1/workspaces/{ws}/readiness', headers=as_('ana')).json())['runs']['state']
    started = client.post('/v1/runs', headers=as_('ana'), json={'workspace_id': ws, 'pool': 'small', 'kind': 'script', 'entrypoint': 'x.py'})
    assert (state == 'ready') == (started.status_code == 201), (state, started.text)


def test_stopped_workspace_reports_unavailable_and_unknown_config(client):
    ws = start_workspace(client)
    assert client.post(f'/v1/workspaces/{ws}/stop', headers=as_('ana')).status_code == 200
    found = checks(client.get(f'/v1/workspaces/{ws}/readiness', headers=as_('ana')).json())
    assert found['workspace']['state'] == 'unavailable'
    assert found['config']['state'] in ('unknown', 'missing')


def test_another_persons_workspace_has_no_readiness(client):
    ws = start_workspace(client)
    assert client.get(f'/v1/workspaces/{ws}/readiness', headers=as_('bob')).status_code == 404
