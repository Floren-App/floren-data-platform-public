"""Airflow listener: a deployed Airflow's DAG runs and failed tasks go to the central workload log.

Registered as an Airflow plugin (entry point fdp_log). It is silent outside a Floren Data Platform
deployment (no FDP_WORKLOAD_ID) and never blocks or fails the scheduler: lines are sent in the background.
"""
import os
import sys

from airflow.listeners import hookimpl
from airflow.plugins_manager import AirflowPlugin

from . import log


def _active() -> bool:
    return bool(os.environ.get("FDP_WORKLOAD_ID"))


def _seconds(dag_run) -> str:
    start, end = getattr(dag_run, "start_date", None), getattr(dag_run, "end_date", None)
    if not start or not end:
        return ""
    seconds = int((end - start).total_seconds())
    return f" after {seconds // 60}m {seconds % 60}s" if seconds >= 60 else f" after {seconds}s"


def _fields(dag_run):
    return {"dag_id": dag_run.dag_id, "dag_run_id": dag_run.run_id, "run_type": str(getattr(dag_run, "run_type", "") or "")}


@hookimpl
def on_dag_run_running(dag_run, msg):
    if _active():
        log.event("dag.started", f"DAG {dag_run.dag_id} started", **_fields(dag_run))


@hookimpl
def on_dag_run_success(dag_run, msg):
    if _active():
        log.event("dag.succeeded", f"DAG {dag_run.dag_id} succeeded{_seconds(dag_run)}", **_fields(dag_run))


@hookimpl
def on_dag_run_failed(dag_run, msg):
    if _active():
        log.event("dag.failed", f"DAG {dag_run.dag_id} failed{_seconds(dag_run)}" + (f": {msg}" if msg else ""),
                  level="error", **_fields(dag_run))


@hookimpl
def on_task_instance_failed(previous_state, task_instance):
    if _active():
        ti = task_instance
        log.event("task.failed", f"Task {ti.task_id} of DAG {ti.dag_id} failed (try {ti.try_number})", level="error",
                  dag_id=ti.dag_id, task_id=ti.task_id, dag_run_id=ti.run_id)


class FDPLogPlugin(AirflowPlugin):
    name = "fdp_log"
    listeners = [sys.modules[__name__]]
