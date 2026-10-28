import base64
import json
import pathlib

import pytest

from app import deps
from app.programs import ArgError, argv_for, coerce_args, json_schema, parse_config, run_script

from .conftest import as_
from .test_api import setup_finance_db, start_workspace

REPO = pathlib.Path(__file__).resolve().parents[3]


def test_airflow_restart_discards_pid_files_and_preserves_metadata(tmp_path):
    import os
    import subprocess
    from app.programs import _AIRFLOW
    home, tools = tmp_path/'home', tmp_path/'bin'
    home.mkdir(); tools.mkdir()
    for name in ('airflow-webserver.pid','airflow-scheduler.pid','airflow-triggerer.pid'):
        (home/name).write_text('42')
    (home/'airflow.db').write_text('persistent metadata')
    airflow = tools/'airflow'
    airflow.write_text('#!/bin/sh\nif [ "$1" = standalone ]; then\n'
        '  test ! -e "$AIRFLOW_HOME/airflow-webserver.pid" || exit 12\n'
        '  test ! -e "$AIRFLOW_HOME/airflow-scheduler.pid" || exit 13\n'
        '  test ! -e "$AIRFLOW_HOME/airflow-triggerer.pid" || exit 14\nfi\n')
    airflow.chmod(0o755)
    result = subprocess.run(['bash','-c',_AIRFLOW.format(project='airflow',port=8501,base='/p/test')],
        cwd=tmp_path, env={**os.environ,'PATH':str(tools)+os.pathsep+os.environ['PATH'],
            'FDP_AIRFLOW_HOME':str(home),'FDP_WORKLOAD_ID':'','FDP_AIRFLOW_LINEAGE':'',
            'FDP_PUBLIC_URL':'http://platform','FDP_AIRFLOW_ROLE':'standalone'}, capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert (home/'airflow.db').read_text() == 'persistent metadata'


@pytest.mark.parametrize('database,parallelism', [
    ('postgresql+psycopg2://airflow:secret@metadata/airflow', '2'),
    ('postgresql://airflow:secret@metadata/airflow', '3'),
])
def test_postgres_airflow_uses_local_executor_and_stops_siblings(tmp_path, database, parallelism):
    import os
    import subprocess
    from app.programs import _AIRFLOW

    tools = tmp_path / 'bin'
    tools.mkdir()
    airflow = tools / 'airflow'
    airflow.write_text('#!/bin/sh\nif [ "$1" = db ]; then exit 0; fi\nprintf \'%s %s %s %s\\n\' "$1" "$AIRFLOW__CORE__EXECUTOR" "$AIRFLOW__CORE__PARALLELISM" "$AIRFLOW__WEBSERVER__WORKERS" >> "$CALL_LOG"\nif [ "$1" = standalone ]; then exit 99; fi\nif [ "$1" = webserver ]; then sleep 1; exit 7; fi\ntrap \'echo "$1 stopped" >> "$CALL_LOG"; exit 0\' TERM INT\nwhile :; do sleep 1; done\n')
    airflow.chmod(0o755)
    python = tools / 'python'
    python.write_text("#!/bin/sh\nif [ \"$1\" = -c ]; then exit 0; fi\n"
                      "trap 'echo lineage-stopped >> \"$CALL_LOG\"; exit 0' TERM INT\n"
                      "while :; do sleep 1; done\n")
    python.chmod(0o755)
    result = subprocess.run(
        ['bash', '-c', _AIRFLOW.format(project='airflow', port=8501, base='/p/test')],
        cwd=tmp_path,
        env={**os.environ, 'PATH':str(tools)+os.pathsep+os.environ['PATH'],
             'FDP_AIRFLOW_HOME':str(tmp_path / 'home'), 'FDP_WORKLOAD_ID':'test',
             'FDP_AIRFLOW_LINEAGE':'', 'FDP_PUBLIC_URL':'http://platform',
             'FDP_AIRFLOW_ROLE':'standalone', 'CALL_LOG':str(tmp_path / 'calls'),
             'AIRFLOW__DATABASE__SQL_ALCHEMY_CONN':database,
             'FDP_AIRFLOW_LOCAL_PARALLELISM':parallelism},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1, result.stderr
    calls = (tmp_path / 'calls').read_text().splitlines()
    for component in ('scheduler', 'webserver', 'triggerer'):
        assert f'{component} LocalExecutor {parallelism} 1' in calls
    assert 'scheduler stopped' in calls and 'triggerer stopped' in calls
    assert 'lineage-stopped' in calls
    assert not any(line.startswith('standalone') for line in calls)


def test_pokemon_manifest_separates_layers_and_declares_two_dashboards():
    folder = REPO / 'examples/pokemon'
    config, errors = parse_config((folder/'fdp.json').read_text())
    assert not errors
    for environment, layer in [('test','Non-Prod'),('prod','Prod')]:
        programs = {name:p for name,p in config.programs.items() if name.endswith('-'+environment)}
        assert len(programs) == 8
        assert len([p for p in programs.values() if p.kind == 'app']) == 2
        for program in programs.values():
            assert program.layer == layer and program.env['POKEMON_ENV'] == environment
            assert (folder/program.entrypoint).exists()
        assert programs['extract-'+environment].data_sources == ['pokemon-landing-'+environment]
        assert programs['models-'+environment].dbt.schema_ == 'pokemon_history'

CONFIG = {
    "$schema": "./.fdp/fdp.schema.json",
    "version": 1,
    "warehouse": "small",
    "programs": {
        "sales-report": {
            "description": "Monthly sales per region",
            "entrypoint": "analysis/sales_report.py",
            "requirements": "requirements.txt",
            "warehouse": "large",
            "data_sources": ["finance-dw"],
            "env": {"REPORT_TITLE": "Sales"},
            "args": [
                {"name": "start_date", "type": "date", "required": True},
                {"name": "region", "type": "choice", "choices": ["North", "South"], "default": "North"},
                {"name": "sample", "type": "number", "min": 0, "max": 1, "default": 1},
                {"name": "top", "type": "integer", "default": 10},
                {"name": "dry_run", "type": "boolean"},
                {"name": "source", "type": "data_source", "default": "finance-dw"},
                {"name": "out_name", "type": "string", "positional": True, "pattern": "[a-z0-9_]+"},
            ],
        },
        "region-summary": {"entrypoint": "analysis/region_summary.R", "requirements": "r-requirements.txt"},
        "explore": {"kind": "notebook", "entrypoint": "notebooks/explore.ipynb"},
        "dashboard": {"kind": "app", "entrypoint": "dashboards/sales.py"},
    },
}


def program():
    cfg, errors = parse_config(json.dumps(CONFIG))
    assert errors == []
    return cfg.programs["sales-report"]


def test_parse_reports_readable_errors():
    _, errors = parse_config('{"programs": {"x": {"entrypoint": "../etc/passwd.py"}}}')
    assert errors == ["programs.x.entrypoint: must be a relative path inside the workspace"]
    _, errors = parse_config('{"programs": {')
    assert errors[0].startswith("fdp.json line 1")
    _, errors = parse_config('{"programs": {"x": {"entrypoint": "a.py", "args": [{"name": "c", "type": "choice"}]}}}')
    assert "needs 'choices'" in errors[0]
    _, errors = parse_config('{"programs": {"Bad Name": {"entrypoint": "a.py"}}}')
    assert "invalid program name" in errors[0]


def test_one_broken_program_does_not_hide_the_others():
    from app.programs import merge_configs

    cfg = json.loads(json.dumps(CONFIG))
    cfg["programs"]["dashboard"]["entrypoint"] = "../outside.py"
    programs, warehouse, errors = merge_configs({"": json.dumps(cfg), "repo-b": '{"programs": {"x": {"entrypoint": "x.R"}}}'})
    assert "dashboard" not in programs and "sales-report" in programs and "repo-b/x" in programs
    assert errors == ["fdp.json: programs.dashboard.entrypoint: must be a relative path inside the workspace"]
    assert warehouse == "small" and programs["repo-b/x"].folder == "repo-b"
    # A file whose top level is broken reports that, and yields nothing.
    _, _, errors = merge_configs({"": '{"version": 2, "programs": {}}'})
    assert errors and errors[0].startswith("fdp.json: version")


def test_runtime_is_inferred():
    cfg, _ = parse_config(json.dumps(CONFIG))
    assert cfg.programs["sales-report"].runtime == "python"
    assert cfg.programs["region-summary"].runtime == "r"


def test_coerce_and_argv():
    args = coerce_args(program(), {"start_date": "2026-01-01", "top": "5", "dry_run": True, "out_name": "q1"},
                       granted={"finance-dw"})
    assert args == {"start_date": "2026-01-01", "region": "North", "sample": 1.0, "top": 5,
                    "dry_run": True, "source": "finance-dw", "out_name": "q1"}
    assert argv_for(program(), args) == [
        "--start-date", "2026-01-01", "--region", "North", "--sample", "1", "--top", "5",
        "--dry-run", "--source", "finance-dw", "q1"]


def test_coerce_collects_every_error():
    with pytest.raises(ArgError) as exc:
        coerce_args(program(), {"region": "East", "sample": 3, "top": "x", "source": "hr-db",
                                "out_name": "Bad!", "bogus": 1}, granted={"finance-dw"})
    assert exc.value.errors == {
        "bogus": "unknown argument",
        "start_date": "required",
        "region": "must be one of North, South",
        "sample": "must be at most 1",
        "top": "must be an integer",
        "source": "data source 'hr-db' is not available to you",
        "out_name": "must match [a-z0-9_]+",
    }


def test_run_script_caches_requirements_and_tees_console():
    script = run_script(run_id="abc", label="x", kind="script", runtime="python", entrypoint="a b.py",
                        argv=["--n", "1"], args={"n": 1}, requirements="requirements.txt")
    assert "python 'a b.py' --n 1" in script
    assert 'python -m venv --system-site-packages "$ENV"' in script and "flock 9" in script
    assert 'tee -a "$RUN_DIR/console.log"' in script
    assert "trap on_stop TERM INT" in script and 'kill -TERM -- "-$PID"' in script
    assert "export FDP_ARGS='{\"n\": 1}'" in script
    r = run_script(run_id="abc", label="x", kind="script", runtime="r", entrypoint="a.R",
                   argv=[], args={}, requirements="renv.lock")
    assert "renv::restore" in r and 'export R_LIBS_USER="$ENV"' in r


def test_schema_file_is_up_to_date():
    # Regenerate with: make schema
    on_disk = json.loads((REPO / "schemas" / "fdp.schema.json").read_text())
    assert on_disk == json_schema()


def put_config(ws_id, owner="ana", config=CONFIG):
    volume = deps.get_orchestrator().executions[f"ws-{ws_id}"].home_volume
    vol = deps.get_orchestrator().volumes.setdefault(volume, {})
    vol["/workspace/fdp.json"] = json.dumps(config).encode()
    return vol


def test_config_endpoint_live_then_cached(client):
    ws_id = start_workspace(client)
    assert client.get(f"/v1/workspaces/{ws_id}/config", headers=as_("ana")).json()["exists"] is False
    put_config(ws_id)
    out = client.get(f"/v1/workspaces/{ws_id}/config", headers=as_("ana")).json()
    assert out["exists"] and out["live"] and out["errors"] == []
    assert sorted(out["config"]["programs"]) == ["dashboard", "explore", "region-summary", "sales-report"]

    client.post(f"/v1/workspaces/{ws_id}/stop", headers=as_("ana"))
    cached = client.get(f"/v1/workspaces/{ws_id}/config", headers=as_("ana")).json()
    assert cached["exists"] and not cached["live"]


def test_run_program_uses_config(client):
    setup_finance_db(client)
    ws_id = start_workspace(client)
    put_config(ws_id)
    r = client.post("/v1/runs", headers=as_("ana"), json={
        "workspace_id": ws_id, "program": "sales-report", "args": {"start_date": "2026-03-01", "out_name": "q1"}})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["pool_name"] == "large" and run["program"] == "sales-report" and run["args"]["top"] == 10
    spec = deps.get_orchestrator().executions[f"run-{run['id']}"]
    assert "--start-date 2026-03-01" in spec.command[2] and "requirements.txt" in spec.command[2]
    assert spec.env["REPORT_TITLE"] == "Sales"
    assert spec.env["FDP_RUN_ID"] == run['id']

    # Warehouse override, and config-level default for programs without one.
    r = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws_id, "program": "sales-report",
                                                          "pool": "xsmall", "args": {"start_date": "2026-03-01"}})
    assert r.json()["pool_name"] == "xsmall"
    r = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws_id, "program": "dashboard"})
    assert r.json()["pool_name"] == "small"


def test_run_program_errors(client):
    ws_id = start_workspace(client, "bob")
    put_config(ws_id, owner="bob")
    r = client.post("/v1/runs", headers=as_("bob"), json={"workspace_id": ws_id, "program": "sales-report"})
    assert r.status_code == 403 and "finance-dw" in r.json()["detail"]["message"]

    ws_ana = start_workspace(client)
    setup_finance_db(client)
    put_config(ws_ana)
    r = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws_ana, "program": "sales-report",
                                                          "args": {"top": "many"}})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == {"start_date": "required", "top": "must be an integer"}
    assert client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws_ana, "program": "nope"}).status_code == 404


def test_console_and_outputs_after_run(client):
    ws_id = start_workspace(client)
    put_config(ws_id)
    run = client.post("/v1/runs", headers=as_("ana"), json={"workspace_id": ws_id, "program": "dashboard"}).json()
    stopped = client.post(f"/v1/runs/{run['id']}/stop", headers=as_("ana")).json()
    assert stopped["status"] == "stopped" and deps.get_orchestrator().last_grace == 5
    # Container gone: console and outputs are read from the workspace volume.
    vol = put_config(ws_id)
    base = f"/workspace/.fdp/runs/{run['id']}"
    vol[f"{base}/console.log"] = b"line 1\nline 2\n"
    vol[f"{base}/outputs/summary.csv"] = b"region,total\nNorth,1\n"
    vol[f"{base}/outputs/plots/chart.png"] = b"\x89PNG..."

    assert client.get(f"/v1/runs/{run['id']}/logs?tail=1", headers=as_("ana")).text == "line 2\n"
    files = client.get(f"/v1/runs/{run['id']}/outputs", headers=as_("ana")).json()
    assert files == [{"path": "plots/chart.png", "size": 7}, {"path": "summary.csv", "size": 21}]
    one = client.get(f"/v1/runs/{run['id']}/outputs/plots/chart.png", headers=as_("ana")).json()
    assert base64.b64decode(one["content_base64"]) == b"\x89PNG..." and not one["truncated"]
    assert client.get(f"/v1/runs/{run['id']}/outputs/../../fdp.json", headers=as_("ana")).status_code in (404, 422)


def test_pools_for_workload_token(client):
    ws_id = start_workspace(client)
    token = deps.get_orchestrator().executions[f"ws-{ws_id}"].env["FDP_SESSION_TOKEN"]
    pools = client.get("/v1/pools", headers={"Authorization": f"Bearer {token}"}).json()
    assert [p["name"] for p in pools if p["name"] != "trino"][:2] == ["xsmall", "small"]
