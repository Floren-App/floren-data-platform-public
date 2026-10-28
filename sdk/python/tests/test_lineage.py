import json
from types import SimpleNamespace

from fdp import lineage


def test_dbt_upload_uses_native_dependencies_and_strips_sensitive_config(tmp_path, monkeypatch):
    (tmp_path / 'manifest.json').write_text(json.dumps({'nodes': {'model.p.sales': {'name': 'sales',
        'depends_on': {'nodes': ['source.p.raw']}, 'compiled_code': 'SECRET SQL',
        'config': {'password': 'SECRET PASSWORD', 'materialized': 'table'},
        'columns': {'id': {'description': 'Identifier', 'meta': {'secret': 'SECRET COLUMN'}}}}}}))
    (tmp_path / 'run_results.json').write_text(json.dumps({'args': {'vars': 'SECRET VARS'},
        'metadata': {'invocation_id':'invocation','generated_at':'2026-10-20T10:00:00Z','secret':'SECRET METADATA'},
        'results': [{'unique_id': 'model.p.sales', 'status': 'success', 'failures': 0, 'execution_time': 1, 'message': 'SECRET QUERY'}]}))
    calls = []
    monkeypatch.setattr(lineage, 'send', lambda kind, body: calls.append((kind, body)))
    lineage.dbt(str(tmp_path))
    kind, body = calls[0]
    assert kind == 'dbt' and 'SECRET' not in json.dumps(body)
    assert body['manifest']['nodes']['model.p.sales']['depends_on']['nodes'] == ['source.p.raw']
    assert body['run_results']['results'][0]['status'] == 'success'
    assert body['run_results']['results'][0]['failures'] == 0
    assert body['run_results']['metadata']['invocation_id'] == 'invocation'


def test_airflow_upload_uses_native_upstreams_datasets_and_rendered_inlets(monkeypatch):
    extract = SimpleNamespace(task_id='extract', upstream_task_ids=set(), inlets=[], outlets=[SimpleNamespace(uri='fdp://lake/finance/raw')])
    transform = SimpleNamespace(task_id='transform', upstream_task_ids={'extract'}, program='finance-models', inlets=[], outlets=[])
    dag = SimpleNamespace(dag_id='finance_daily', schedule_interval='@daily', tasks=[extract, transform])
    calls = []
    monkeypatch.setattr(lineage, 'send', lambda kind, body: calls.append((kind, body)))
    lineage.airflow_dag(dag, transform, inlets=[SimpleNamespace(uri='fdp://lake/finance/raw')], outlets=[])
    body = calls[0][1]
    assert body['tasks'][1]['upstream_task_ids'] == ['extract']
    assert body['tasks'][1]['program'] == 'finance-models'
    assert body['tasks'][1]['inlets'] == [{'uri': 'fdp://lake/finance/raw'}]
    assert lineage.reference('https://user:secret@evil.test/?token=secret') is None
