"""Bounded, read-only DAG history from a workload's own Airflow metadata store."""
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from .config import get_settings
from .airflow_db import keys_path, shared_database_url


# Runs inside a managed layer's artifact holder: a fixed, read-only query on the standalone
# metadata file. Arguments are passed as argv, never interpolated into SQL or code.
LAYER_QUERY = r'''
import json, sqlite3, sys
path, limit, dag_id = sys.argv[1], int(sys.argv[2]), sys.argv[3]
conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=3)
cols = {row[1] for row in conn.execute("PRAGMA table_info(dag_run)")}
logical = "logical_date" if "logical_date" in cols else "execution_date"
where, params = ("WHERE dag_id = ?", [dag_id]) if dag_id else ("", [])
rows = conn.execute("SELECT dag_id, run_id, state, " + logical + ", start_date, end_date FROM dag_run " + where +
                    " ORDER BY COALESCE(end_date, start_date, " + logical + ") DESC, run_id DESC LIMIT ?", params + [limit]).fetchall()
dags = [r[0] for r in conn.execute("SELECT DISTINCT dag_id FROM dag_run ORDER BY dag_id LIMIT 100")]
keys = ("dag_id", "run_id", "state", "logical_date", "start_date", "end_date")
print(json.dumps({"runs": [dict(zip(keys, r)) for r in rows], "dags": dags}))
'''


# Runs inside a layer deployment's own metadata database pod over its local socket: a fixed read-only
# query. The DAG filter and limit arrive as argv and are quoted by psql (:'dag'), never spliced in.
LAYER_PG_QUERY = r'''export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=3000"
psql -X -q -A -t -v ON_ERROR_STOP=1 -U airflow -d airflow -v dag="$1" -v lim="$2" <<'SQL'
WITH r AS (
  SELECT dag_id, run_id, state, COALESCE(to_jsonb(d)->>'logical_date', to_jsonb(d)->>'execution_date')::timestamptz AS logical,
         start_date, end_date FROM dag_run d WHERE :'dag' = '' OR dag_id = :'dag'
), stamp AS (SELECT 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'::text AS f)
SELECT json_build_object(
  'runs', COALESCE((SELECT json_agg(json_build_object('dag_id', dag_id, 'run_id', run_id, 'state', state,
      'logical_date', to_char(logical AT TIME ZONE 'UTC', f), 'start_date', to_char(start_date AT TIME ZONE 'UTC', f),
      'end_date', to_char(end_date AT TIME ZONE 'UTC', f)))
    FROM (SELECT * FROM r ORDER BY COALESCE(end_date, start_date, logical) DESC, run_id DESC LIMIT :lim) x, stamp), '[]'),
  'dags', COALESCE((SELECT json_agg(dag_id) FROM (SELECT DISTINCT dag_id FROM dag_run ORDER BY dag_id LIMIT 100) y), '[]'));
SQL
'''


def layer_distributed_runs(orch, namespace, pub, limit, dag_id):
    import json
    code, out = orch.execution_exec(f"{namespace}/pub-{pub.id}-metadata",
                                   ["sh", "-c", LAYER_PG_QUERY, "history", dag_id, str(int(limit))])
    if code != 0 or not out.strip():
        raise ValueError("metadata store unavailable")
    result = json.loads(out.strip().splitlines()[-1])
    for row in result["runs"]:
        for key in ("logical_date", "start_date", "end_date"):
            row[key] = _utc(row[key])
    return result


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime) and not value.tzinfo:
        value = value.replace(tzinfo=timezone.utc)
    return value


def layer_runs(orch, namespace, pub, limit, dag_id):
    import json
    code, out = orch.holder_exec(namespace, ["python", "-c", LAYER_QUERY,
                                             f"/artifacts/airflow/{pub.id}/airflow.db", str(int(limit)), dag_id])
    if code != 0:
        raise ValueError("metadata store unavailable")
    result = json.loads(out)
    for row in result["runs"]:
        for key in ("logical_date", "start_date", "end_date"):
            row[key] = _utc(row[key])
    return result


def recent_runs(pub, store, limit=5, dag_id="", artifacts=None):
    settings = get_settings()
    mode = ((pub.program_def or {}).get("airflow_settings") or {}).get("mode", "standalone")
    keys = (store.read(keys_path(pub.id)) or {}) if store is not None else {}
    shared = keys.get('metadata_backend') == 'shared-postgres'
    if mode == "distributed" and getattr(artifacts, "namespace", None) and not shared:
        return layer_distributed_runs(artifacts.orch, artifacts.namespace, pub, limit, dag_id)
    if shared or (mode == "distributed" and not getattr(artifacts, "namespace", None)):
        if not settings.airflow_metadata_url or not keys.get("password"):
            raise ValueError("metadata store unavailable")
        url = shared_database_url(pub.id, keys, driver="postgresql+psycopg")
        connect_args = {"connect_timeout": 3, "options": "-c default_transaction_read_only=on -c statement_timeout=3000"}
    elif getattr(artifacts, "namespace", None):
        return layer_runs(artifacts.orch, artifacts.namespace, pub, limit, dag_id)
    else:
        path = Path(settings.data_dir) / "airflow" / pub.id / "airflow.db"
        # mode=ro prevents creating an empty database for an unstarted workload.
        url = "sqlite:///" + path.resolve().as_uri() + "?mode=ro&uri=true"
        connect_args = {"timeout": 3}
    engine = create_engine(url, poolclass=NullPool, connect_args=connect_args)
    try:
        with engine.connect() as conn:
            table = Table("dag_run", MetaData(), autoload_with=conn)
            logical = table.c.get("logical_date")
            if logical is None:
                logical = table.c.execution_date
            changed = func.coalesce(table.c.end_date, table.c.start_date, logical)
            stmt = select(table.c.dag_id, table.c.run_id, table.c.state, logical.label("logical_date"),
                table.c.start_date, table.c.end_date).order_by(changed.desc(), table.c.run_id.desc()).limit(limit)
            if dag_id:
                stmt = stmt.where(table.c.dag_id == dag_id)
            rows = [dict(row) for row in conn.execute(stmt).mappings()]
            dags = list(conn.scalars(select(table.c.dag_id).distinct().order_by(table.c.dag_id).limit(100)))
        for row in rows:
            for key in ("logical_date", "start_date", "end_date"):
                value = row[key]
                if isinstance(value, datetime) and not value.tzinfo:
                    row[key] = value.replace(tzinfo=timezone.utc)
        return {"runs": rows, "dags": dags}
    finally:
        engine.dispose()
