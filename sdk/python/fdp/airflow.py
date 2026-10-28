"""Airflow operator: run Floren Data Platform programs and dbt projects on warehouses from DAGs.

    from fdp.airflow import FDPRunOperator

    with DAG("finance_daily", schedule="@daily", start_date=pendulum.datetime(2026, 1, 1), catchup=False):
        models = FDPRunOperator(task_id="models", program="finance-models",
                                      args={"command": "build", "select": "tag:daily"})
        report = FDPRunOperator(task_id="report", program="sales-report",
                                      args={"start_date": "{{ ds }}"}, warehouse="large")
        models >> report

program=      a program in the same repository (deployed Airflow) or workspace (test deployment)
workload=  any deployed program you may use, by its address name

Airflow only orchestrates: the work runs on the warehouse, its console streams into the task log,
the task fails when the run fails, and clearing/killing the task stops the run.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from airflow.exceptions import AirflowException
from airflow.models import BaseOperator

import fdp


class FDPRunOperator(BaseOperator):
    template_fields = ("program", "workload", "args", "warehouse")
    ui_color = "#0078d4"
    ui_fgcolor = "#ffffff"

    def __init__(self, *, program: Optional[str] = None, workload: Optional[str] = None,
                 args: Optional[Dict[str, Any]] = None, warehouse: Optional[str] = None,
                 poll_seconds: float = 5, **kwargs: Any):
        super().__init__(**kwargs)
        if bool(program) == bool(workload):
            raise ValueError("give exactly one of program= or workload=")
        self.program, self.workload = program, workload
        self.args, self.warehouse, self.poll_seconds = args or {}, warehouse, poll_seconds
        self._run_id: Optional[str] = None

    def execute(self, context: Any) -> str:
        try:
            from .lineage import airflow_dag
            airflow_dag(self.dag)
        except Exception:
            self.log.warning("Floren Data Platform DAG lineage upload failed; continuing the task")
        run = fdp.orchestrate(program=self.program, workload=self.workload,
                                    args=self.args, warehouse=self.warehouse)
        self._run_id = run["id"]
        self.log.info("Floren Data Platform run %s on warehouse %s (%s %s)", run["id"], run["pool_name"],
                      run.get("runtime"), run.get("runtime_version"))
        printed = 0
        while True:
            logs = fdp.run_logs(run["id"])
            for line in logs[printed:].splitlines():
                self.log.info("%s", line)
            printed = len(logs)
            status = fdp.run_status(run["id"])
            if status not in ("queued", "pending", "running"):
                break
            time.sleep(self.poll_seconds)
        if status != "succeeded":
            raise AirflowException(f"Floren Data Platform run {run['id']} {status}")
        return run["id"]

    def on_kill(self) -> None:
        if self._run_id:
            self.log.info("stopping Floren Data Platform run %s", self._run_id)
            fdp.stop_run(self._run_id)
