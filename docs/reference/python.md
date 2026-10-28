# Python function reference

Generated from public SDK function signatures and docstrings. See the [Python guide](../python-sdk.md) for authentication and examples.

## `args`

```python
args()
```

Typed argument values for this run (from fdp.json + the run form). Empty outside a run.

## `output_dir`

```python
output_dir()
```

Where to write files that should show up as run outputs (cwd outside a run).

## `sources`

```python
sources()
```

Data sources your groups have been granted.

## `credentials`

```python
credentials(name: str, token: Optional[str]=None, schema: Optional[str]=None, viewer_token: Optional[str]=None)
```

Raw connection info: {'kind', 'config', 'credentials'}. Prefer connect().

## `connect`

```python
connect(name: str, token: Optional[str]=None, schema: Optional[str]=None, viewer_token: Optional[str]=None, **kwargs: Any)
```

Returns a ready client: SQLAlchemy engine for SQL sources, boto3 client for S3.

token: a service token, to connect as that service account (e.g. from outside Floren Data Platform).

## `run`

```python
run(entrypoint: str, pool: str='', kind: str='script')
```

Run a script/notebook/app from this workspace on a warehouse (default: the platform's default warehouse).

## `run_program`

```python
run_program(program: str, warehouse: str='', **arguments: Any)
```

Run a program defined in fdp.json, e.g. run_program("sales-report", start_date="2026-01-01").

## `programs`

```python
programs()
```

This workspace's fdp.json as the control plane sees it (validated).

## `runs`

```python
runs()
```

See the source for details.

## `orchestrate`

```python
orchestrate(program: Optional[str]=None, workload: Optional[str]=None, args: Optional[Dict[str, Any]]=None, warehouse: Optional[str]=None)
```

Start a run from an orchestrator (used by fdp.airflow.FDPRunOperator).

## `run_logs`

```python
run_logs(run_id: str)
```

See the source for details.

## `run_status`

```python
run_status(run_id: str)
```

See the source for details.

## `stop_run`

```python
stop_run(run_id: str)
```

See the source for details.

## `files`

```python
files(name: str, token: Optional[str]=None, *, viewer_token: Optional[str]=None)
```

Files in an S3-compatible data source (RustFS, MinIO, S3), within its bucket/prefix:
files("lake-files").read_json("x.json"), .write_text(...), .list("reports/").

## `query`

```python
query(source: str, sql: str, parameters=None, *, viewer_token: Optional[str]=None, max_rows: int=1000, token: Optional[str]=None)
```

Shared caller-mode apps: pass the gateway's X-FDP-Viewer header per request.

Keep this token in viewer-local state. Do not cache query results across viewer contexts.
The server evaluates the viewer's current permissions and keeps credentials server-side.

## `certificate`

```python
certificate(var: str)
```

Write a certificate secret (fdp.json "secrets": {"VAR": "me/name"}) to private files for TLS clients.

The program receives the certificate as $VAR, the private key as $VAR_KEY and the CA chain as $VAR_CA.
Most clients want files: requests.get(url, cert=(c.cert, c.key), verify=c.ca or True).

## `api`

```python
api(name: str, token: Optional[str]=None)
```

fdp.api("crm").get("/customers/7").json()

## `measure_metric`

```python
measure_metric(asset_id, metric_id, token=None, viewer_token=None)
```

Evaluate a saved SQL KPI through governed Trino; return its observation.

## `report_metric`

```python
report_metric(asset_id, metric_id, value, *, numerator=None, denominator=None, observed_at=None, idempotency_key=None, token=None)
```

Report a finite numeric result. Requires Contributor; thresholds run server-side.

## `observe_dataset`

```python
observe_dataset(asset_id, *, kind='checked', idempotency_key, observed_at=None, token=None)
```

Record a successful check, materialization, or source watermark separately.
