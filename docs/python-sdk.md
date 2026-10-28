# Python SDK

Workspace and compute images include `fdp`. They receive platform context and a
scoped execution credential automatically. Use managed identities for published
workloads rather than embedding database passwords or personal tokens.

## Arguments, output and SQL

```python
from pathlib import Path
import fdp

args = fdp.args()
result = fdp.query("warehouse", "SELECT * FROM reporting.cards_current LIMIT 25")
Path(fdp.output_dir(), "preview.json").write_text(__import__("json").dumps(result, default=str))
```

Replace `warehouse` with a registered integration you can access. `fdp.query`
returns the API result envelope; use its column/row fields as appropriate.
For pandas/SQLAlchemy usage:

```python
import pandas as pd
import fdp

df = pd.read_sql("SELECT * FROM reporting.cards_current LIMIT 25", fdp.connect("warehouse"))
```

Shared Trino execution uses positional parameters. Do not pass a named dictionary
to the Trino DBAPI where it expects a list/tuple. See [SQL execution](query-execution.md).

## Managed files and APIs

```python
import fdp

files = fdp.files("landing")
files.write_json("batches/example.json", [{"id": "one"}, {"id": "two"}])
records = files.read_json("batches/example.json")
```

The caller needs the applicable storage role. Keep immutable batches in S3 and
split JSON lists into rows when loading raw tables, not into one object per record.
`fdp.api(name)` accesses a registered API under its own Consumer permission; see
[API management](api-management.md). SDK file/API calls and governed queries can
supply lineage observations; see [catalog lineage](catalog.md).

## Caller-mode dashboards

Resolve the gateway's viewer token for each viewer session and pass it with each
data request using `viewer_token=`. Do not cache it or its query results across
viewers. Calling the ordinary fixed-identity connection from a shared dashboard
does not become caller-authorized merely because the app displays the user's name.

## Other capabilities

The SDK supports running published programs, polling logs/status, output files,
certificates, metric observations and dataset freshness evidence. Use the
[generated function reference](reference/python.md) for current signatures.
For Airflow, prefer the FDP operators and orchestration helpers so caller/run/task
context can be recorded instead of invoking an unauthenticated subprocess.

Outside FDP, install `sdk/python` from the source and explicitly configure the API
address and an authorized credential. A local shell does not inherit a managed
workspace or publication identity.
