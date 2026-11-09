"""An execution credential reaches only the runs of the context it was issued for.

Owning two workspaces does not let one workspace's editor read, open or control the other's runs; interactive
access to a person's own runs is unchanged. A deployed orchestrator keeps access to the runs it starts.
"""
from app.auth import mint_token
from .conftest import as_
from .test_api import start_workspace

RUN = {'pool': 'small', 'kind': 'script', 'entrypoint': 'x.py'}


def bearer(**context):
    return {'Authorization': 'Bearer ' + mint_token('execution', 'ana', 3600, **context)}


def two_workspaces_with_runs(client):
    first = start_workspace(client)
    second = client.post('/v1/workspaces', headers=as_('ana'), json={'name': 'second', 'editor': 'vscode'}).json()['id']
    assert client.post(f'/v1/workspaces/{second}/start', headers=as_('ana')).json()['status'] == 'running'
    runs = []
    for ws in (first, second):
        response = client.post('/v1/runs', headers=as_('ana'), json={'workspace_id': ws, **RUN})
        assert response.status_code == 201, response.text
        runs.append(response.json()['id'])
    return (first, second), runs


def reachable(client, headers, run_id):
    """Every run route answers 404 for a run outside the caller's context."""
    reads = [client.get(f'/v1/runs/{run_id}', headers=headers),
             client.get(f'/v1/runs/{run_id}/logs', headers=headers),
             client.get(f'/v1/runs/{run_id}/outputs', headers=headers),
             client.get(f'/v1/runs/{run_id}/outputs/result.txt', headers=headers)]
    controls = [client.post(f'/v1/runs/{run_id}/open', headers=headers),
                client.post(f'/v1/runs/{run_id}/retry', headers=headers),
                client.post(f'/v1/runs/{run_id}/stop', headers=headers)]
    return [r.status_code for r in reads + controls]


def test_workspace_credential_cannot_reach_another_workspace_of_the_same_person(client):
    (first, second), (own, other) = two_workspaces_with_runs(client)
    editor = bearer(ws=first)
    assert set(reachable(client, editor, other)) == {404}
    listed = {r['id'] for r in client.get('/v1/runs', headers=editor).json()}
    assert own in listed and other not in listed
    assert other not in {r['id'] for r in client.get(f'/v1/runs?workspace_id={second}', headers=editor).json()}
    assert client.get(f'/v1/runs/{own}', headers=editor).status_code == 200


def test_run_credential_stays_within_its_workspace(client):
    (first, _), (own, other) = two_workspaces_with_runs(client)
    job = bearer(ws=first, run=own)
    assert set(reachable(client, job, other)) == {404}
    assert client.get(f'/v1/runs/{own}', headers=job).status_code == 200


def test_interactive_owner_still_reaches_both_workspaces(client):
    _, runs = two_workspaces_with_runs(client)
    listed = {r['id'] for r in client.get('/v1/runs', headers=as_('ana')).json()}
    assert set(runs) <= listed
    for run_id in runs:
        assert client.get(f'/v1/runs/{run_id}', headers=as_('ana')).status_code == 200
