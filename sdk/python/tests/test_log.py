"""fdp.log: lines print, and inside the platform go to the central log in the background."""
import logging

import fdp
from fdp import log


def collect(monkeypatch):
    sent = []
    monkeypatch.setattr(log, "_send", lambda entries: sent.extend(entries))
    return sent


def test_lines_print_and_are_sent_with_levels_and_fields(monkeypatch, capsys):
    monkeypatch.setenv("FDP_API", "http://fdp.test")
    monkeypatch.setenv("FDP_SESSION_TOKEN", "token")
    sent = collect(monkeypatch)
    fdp.log.info("loaded rows", table="sales", rows=120)
    fdp.log.warning("3 rows without a region")
    try:
        raise ValueError("boom")
    except ValueError:
        fdp.log.exception("upload failed", attempt=2)
    log.flush()
    assert [(e["level"], e["message"]) for e in sent] == [("info", "loaded rows"), ("warning", "3 rows without a region"),
                                                          ("error", "upload failed")]
    assert sent[0]["fields"] == {"table": "sales", "rows": 120} and sent[0]["at"].endswith("+00:00")
    assert "ValueError: boom" in sent[2]["fields"]["traceback"] and sent[2]["fields"]["attempt"] == 2
    # Nothing the program sends names its run or workload: the platform stamps those.
    assert not any({"run_id", "workload_id", "commit_sha"} & set(e) for e in sent)
    assert "[INFO] loaded rows table=sales rows=120" in capsys.readouterr().err


def test_python_logging_goes_there_too(monkeypatch):
    monkeypatch.setenv("FDP_API", "http://fdp.test")
    monkeypatch.setenv("FDP_SESSION_TOKEN", "token")
    sent = collect(monkeypatch)
    logger = logging.getLogger("etl")
    logger.addHandler(fdp.log.handler())
    logger.setLevel(logging.INFO)
    try:
        logger.info("step %d done", 3)
        logger.debug("not sent: below the handler's level")
        logging.getLogger("httpx").addHandler(fdp.log.handler())
        logging.getLogger("httpx").warning("never sent: the SDK's own HTTP client")
    finally:
        logger.handlers.clear()
        logging.getLogger("httpx").handlers.clear()
    log.flush()
    assert [(e["level"], e["message"], e["fields"]["logger"]) for e in sent] == [("info", "step 3 done", "etl")]


def test_outside_the_platform_lines_only_print(monkeypatch, capsys):
    monkeypatch.delenv("FDP_API", raising=False)
    sent = collect(monkeypatch)
    fdp.log.error("local run")
    log.flush()
    assert sent == [] and "[ERROR] local run" in capsys.readouterr().err


def test_a_failing_log_service_never_raises(monkeypatch):
    monkeypatch.setenv("FDP_API", "http://127.0.0.1:9")  # nothing listens here
    monkeypatch.setenv("FDP_SESSION_TOKEN", "token")
    fdp.log.info("still fine")
    log.flush(timeout=15)


def test_airflow_listener_logs_dag_runs_and_failed_tasks_only_in_deployments(monkeypatch):
    import importlib
    import sys
    from datetime import datetime, timedelta
    from types import ModuleType, SimpleNamespace
    listeners, plugins = ModuleType("airflow.listeners"), ModuleType("airflow.plugins_manager")
    listeners.hookimpl = lambda f: f
    plugins.AirflowPlugin = type("AirflowPlugin", (), {})
    for name, module in (("airflow", ModuleType("airflow")), ("airflow.listeners", listeners), ("airflow.plugins_manager", plugins)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "fdp.airflow_listener", raising=False)
    listener = importlib.import_module("fdp.airflow_listener")
    events = []
    monkeypatch.setattr(log, "event", lambda name, message, level="info", **fields: events.append((name, level, message, fields)))
    start = datetime(2026, 10, 31, 9, 0)
    run = SimpleNamespace(dag_id="pokemon", run_id="scheduled__1", run_type="scheduled", start_date=start, end_date=start + timedelta(seconds=75))
    monkeypatch.delenv("FDP_WORKLOAD_ID", raising=False)
    listener.on_dag_run_running(run, "")
    assert events == []  # a workspace's own Airflow: nothing is sent
    monkeypatch.setenv("FDP_WORKLOAD_ID", "wl1")
    listener.on_dag_run_running(run, "")
    listener.on_dag_run_failed(run, "task load failed")
    listener.on_task_instance_failed("running", SimpleNamespace(task_id="load", dag_id="pokemon", try_number=2, run_id="scheduled__1"))
    assert [(e[0], e[1]) for e in events] == [("dag.started", "info"), ("dag.failed", "error"), ("task.failed", "error")]
    assert events[1][2] == "DAG pokemon failed after 1m 15s: task load failed" and events[0][3]["dag_run_id"] == "scheduled__1"
    assert listener.FDPLogPlugin.listeners == [listener]
