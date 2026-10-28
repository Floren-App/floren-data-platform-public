#!/usr/bin/env bash
# Disposable full SQL integration suite. Requires Docker, the control-plane venv and SDK sql extra.
set -euo pipefail
cd "$(dirname "$0")/../../../.."
sql_test_project="fdp-sql-test-$$"
sql_test_compose=(docker compose -p "$sql_test_project" -f services/control-plane/tests/sql-integration/compose.yml)
cleanup() { "${sql_test_compose[@]}" down --volumes >/dev/null 2>&1; }
trap cleanup EXIT
"${sql_test_compose[@]}" up -d
export FDP_TEST_TRINO_URL="http://$("${sql_test_compose[@]}" port trino 8080)"
export FDP_TEST_PG_URL="postgresql://warehouse_admin:warehouse_admin@$("${sql_test_compose[@]}" port warehouse 5432)/finance"
export FDP_TEST_S3_URL="http://$("${sql_test_compose[@]}" port rustfs 9000)"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/fdp-test-pycache}"
services/control-plane/.venv/bin/python - <<'PY'
import os, time, httpx, psycopg
for _ in range(60):
    try:
        with psycopg.connect(os.environ['FDP_TEST_PG_URL'], autocommit=True) as conn:
            conn.execute('CREATE SCHEMA IF NOT EXISTS cp_sql_test')
        response = httpx.get(os.environ['FDP_TEST_TRINO_URL'] + '/v1/info', timeout=2)
        if response.status_code in (200,401):
            break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit('Disposable SQL backends did not become ready')
PY
export FDP_TEST_CP_URL="${FDP_TEST_PG_URL/postgresql:/postgresql+psycopg:}?options=-csearch_path%3Dcp_sql_test"
services/control-plane/.venv/bin/pytest -q services/control-plane/tests/test_shared_sql.py services/control-plane/tests/test_data_browser.py services/control-plane/tests/test_trino_integration.py
