#!/usr/bin/env bash
# Run control-plane regressions against a disposable PostgreSQL database, never the local app DB.
set -euo pipefail
cd "$(dirname "$0")/../../../.."
cp_test_container="$(docker run --rm -d -p 127.0.0.1::5432 \
  -e POSTGRES_USER=fdp_test -e POSTGRES_PASSWORD=fdp_test \
  -e POSTGRES_DB=fdp_test postgres:16-alpine)"
cleanup() { docker rm -f "$cp_test_container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cp_test_address="$(docker port "$cp_test_container" 5432/tcp)"
export FDP_TEST_CP_URL="postgresql+psycopg://fdp_test:fdp_test@${cp_test_address}/fdp_test"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/fdp-test-pycache}"
services/control-plane/.venv/bin/python - <<'PY'
import os, time, psycopg
url = os.environ['FDP_TEST_CP_URL'].replace('postgresql+psycopg:', 'postgresql:')
for attempt in range(60):
    try:
        with psycopg.connect(url, connect_timeout=2):
            break
    except psycopg.OperationalError:
        time.sleep(.5)
else:
    raise SystemExit('Disposable PostgreSQL did not become ready')
PY
services/control-plane/.venv/bin/python -m pytest -q services/control-plane/tests "$@"
