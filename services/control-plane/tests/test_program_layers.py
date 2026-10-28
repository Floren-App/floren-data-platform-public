import json

from app import deps, layers
from app.db import SessionLocal
from app.models import Workload, Run
from app.programs import merge_configs

from .conftest import as_
from .test_api import start_workspace
from .test_programs import put_config
from .test_deploying import deploy, push, reconcile
from .test_deploying import repo  # noqa: F401


def test_file_layer_defaults_and_program_overrides_survive_partial_validation():
    programs, _, errors = merge_configs({'pokemon': json.dumps({
        'layer':' Prod ', 'programs':{
            'prod':{'entrypoint':'x.py'},
            'test':{'layer':'Non-Prod','entrypoint':'x.py'},
            'invalid':{'layer':'  ','entrypoint':'x.py'}}})})
    assert programs['pokemon/prod'].program.layer == 'Prod'
    assert programs['pokemon/test'].program.layer == 'Non-Prod'
    assert 'pokemon/invalid' not in programs and len(errors)==1


def test_workspace_lists_and_runs_only_matching_programs_live_and_cached_even_for_admin(client):
    ws = start_workspace(client, 'admin')
    put_config(ws, owner='admin', config={'warehouse':'small', 'programs':{
        'test':{'layer':'Non-Prod','entrypoint':'x.py'},
        'legacy':{'entrypoint':'x.py'},
        'prod':{'layer':'Prod','entrypoint':'x.py'},
        'prod-orchestration':{'layer':layers.PROD,'kind':'airflow','entrypoint':'airflow'}}})
    for stopped in (False, True):
        if stopped:
            client.post('/v1/workspaces/'+ws+'/stop', headers=as_('admin'))
        result = client.get('/v1/workspaces/'+ws+'/config', headers=as_('admin')).json()
        assert result['live'] is not stopped
        assert set(result['config']['programs']) == {'test','legacy'} and not result['errors']
        before = len(client.get('/v1/runs', headers=as_('admin')).json())
        for program in ('prod','prod-orchestration'):
            denied = client.post('/v1/runs', headers=as_('admin'), json={'workspace_id':ws,'program':program})
            assert denied.status_code == 404 and 'layer' in denied.text
        assert len(client.get('/v1/runs', headers=as_('admin')).json()) == before
    allowed = client.post('/v1/runs', headers=as_('admin'), json={'workspace_id':ws,'program':'test'})
    assert allowed.status_code == 201, allowed.text
    with SessionLocal() as db:
        assert db.get(Run, allowed.json()['id']).environment_id == layers.NON_PROD


def test_unknown_layer_reports_a_config_error_without_hiding_valid_programs(client):
    ws = start_workspace(client)
    put_config(ws, config={'layer':layers.NON_PROD, 'programs':{
        'valid':{'entrypoint':'x.py'}, 'typo':{'layer':'Prodd','entrypoint':'x.py'}}})
    result = client.get('/v1/workspaces/'+ws+'/config', headers=as_('ana')).json()
    assert set(result['config']['programs']) == {'valid'}
    assert len(result['errors']) == 1 and 'unknown layer' in result['errors'][0] and 'Prodd' in result['errors'][0]


def edit_program_layer(work, layer):
    path = work/'fdp.json'
    cfg = json.loads(path.read_text())
    cfg['programs']['region-summary']['layer'] = layer
    path.write_text(json.dumps(cfg))


def test_deploy_requires_matching_declared_layer_and_gitops_cannot_change_it(client, repo):
    push(repo['work'], 'Declare production layer', lambda work:edit_program_layer(work,'Prod'))
    result = deploy(client, repo, user='admin', program='region-summary', slug='wrong-layer', layer_id=layers.NON_PROD)
    assert result.status_code == 422 and 'requires layer' in result.text
    result = deploy(client, repo, user='admin', program='region-summary', slug='right-layer', layer_id=layers.PROD)
    assert result.status_code == 201, result.text
    original = result.json()
    push(repo['work'], 'Try to change deployed layer', lambda work:edit_program_layer(work,'Non-Prod'))
    reconcile()
    with SessionLocal() as db:
        pub = db.get(Workload,original['id'])
        assert pub.deployed_sha == original['deployed_sha']
        assert 'requires layer' in pub.sync_error and pub.program_def['layer']=='Prod'


def test_airflow_cannot_start_other_layer_program_from_same_repository(client, repo):
    def edit(work):
        path = work/'fdp.json'
        path.write_text(json.dumps({'warehouse':'small','layer':'Non-Prod','programs':{
            'pipeline':{'kind':'airflow','entrypoint':'airflow'},
            'test-task':{'entrypoint':'x.py'},
            'prod-task':{'layer':'Prod','entrypoint':'x.py'}}}))
    push(repo['work'], 'Layer-bound orchestration', edit)
    result = deploy(client, repo, user='admin', program='pipeline', slug='pipeline',layer_id=layers.NON_PROD)
    assert result.status_code == 201, result.text
    pub = result.json()
    spec = deps.get_orchestrator().executions[f"pub-{pub['id']}-{pub['deployed_sha'][:7]}"]
    headers = {'Authorization':'Bearer '+spec.env['FDP_SESSION_TOKEN']}
    denied = client.post('/v1/orchestrate/runs', headers=headers,json={'program':'prod-task'})
    assert denied.status_code == 403 and 'requires layer' in denied.text
    allowed = client.post('/v1/orchestrate/runs', headers=headers,json={'program':'test-task'})
    assert allowed.status_code == 201, allowed.text
