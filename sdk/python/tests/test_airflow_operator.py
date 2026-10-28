import importlib.util
import logging
from pathlib import Path
from types import ModuleType

import fdp
from fdp import lineage


def test_operator_waits_for_queued_runs_without_starting_duplicates(monkeypatch):
    import sys
    exceptions, models = ModuleType('airflow.exceptions'), ModuleType('airflow.models')
    exceptions.AirflowException = type('AirflowException', (Exception,), {})

    class Operator:
        def __init__(self, **kwargs):
            self.log = logging.getLogger('operator-test')
            self.dag = object()

    models.BaseOperator = Operator
    monkeypatch.setitem(sys.modules,'airflow',ModuleType('airflow'))
    monkeypatch.setitem(sys.modules,'airflow.exceptions',exceptions)
    monkeypatch.setitem(sys.modules,'airflow.models',models)
    spec = importlib.util.spec_from_file_location('tested_airflow',Path(fdp.__file__).with_name('airflow.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statuses, calls = iter(['queued','pending','running','succeeded']), []
    monkeypatch.setattr(lineage,'airflow_dag',lambda *args: None)
    monkeypatch.setattr(fdp,'orchestrate',lambda **args: calls.append(args) or {'id':'run','pool_name':'jobs'})
    monkeypatch.setattr(fdp,'run_status',lambda run: next(statuses))
    monkeypatch.setattr(fdp,'run_logs',lambda run: '')
    monkeypatch.setattr(module.time,'sleep',lambda seconds: None)
    assert module.FDPRunOperator(workload='extract').execute(None) == 'run'
    assert len(calls) == 1
