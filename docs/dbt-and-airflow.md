> Governed SQL now uses dbt-trino for both PostgreSQL and Iceberg. Profiles authenticate to the shared gateway and contain no native database/storage credentials. See [shared SQL](query-execution.md); older adapter/materialization instructions below describe legacy mode.

# dbt and Airflow

Both are program kinds in `fdp.json` ([reference](fdp-json.md)). They get everything
other programs have: runs on warehouses with a live console, runner panels in the hub, VS Code and
RStudio, deploying from git with GitOps, and per-identity data access.

## dbt projects

```json
"finance-models": {
  "kind": "dbt",
  "entrypoint": "dbt/finance",
  "dbt": { "data_source": "finance-dw", "schema": "analytics" },
  "dbt_version": "1.9",
  "warehouse": "small"
}
```

| Field | Meaning |
|---|---|
| `entrypoint` | Folder containing `dbt_project.yml`. |
| `dbt.data_source` | The Floren Data Platform data source dbt builds in. Grant the execution identity Admin on its target schema and Reader on input schemas through RBAC; register/provision development schemas first. See [RBAC](rbac.md). |
| `dbt.schema` | Production schema (deployed runs). Default `analytics`. |
| `dbt.dev_schema` | Schema for workspace runs. Default `dbt_{user}`, so everyone develops in their own schema and never touches production. |
| `dbt.threads` | dbt threads (default 4). |
| `dbt_version` | Version from the runtime catalog (default: the catalog's default). |

Both PostgreSQL and Iceberg use `dbt-trino` with the shared governed SQL executor.
Floren Data Platform generates `profiles.yml` at run time from the connection metadata and a
execution token. Native database and storage credentials remain in the infrastructure.
The project's `profile:` name is used automatically.

Older demo projects used a separate `finance-dw-transform` integration and dbt-postgres 1.8.
Update those projects to `finance-dw`, use the Trino runtime, and grant the execution
identity Reader on `public` and Admin on `analytics`. Use portable `cast(...)` expressions
and table materializations. Existing native PostgreSQL staging views are blocked on the
governed path; migrate those generated views to tables before rebuilding, preserving their
definitions, rows, ownership and grants. Updating files in `examples/workspace` alone does
not update an existing Gitea repository: commit the changes to the workload's branch
and sync the workload before rerunning it.

**The run form is built in:** command (`build`, `run`, `test`, `seed`, `snapshot`, `docs`), select,
exclude, full refresh and vars.

**Docs with lineage:** every run, including a failed one, ends with `dbt docs generate --static`.
- **Workspace runs:** **Open dbt docs** on the run (hub) or **docs** in the VS Code runner opens the
  single-page docs with the lineage graph.
- **Deployed dbt projects:** the docs are served at `https://<fdp host>/p/<name>/` for everyone
  allowed. A new commit goes live only once its docs generate against production, and every
  production run refreshes them.

**In a workspace terminal:** `fdp dbt-profile --source finance-dw --write` writes
`~/.dbt/profiles.yml` for your dev schema, so `dbt compile`, `dbt show` and the like work
interactively. Heavier work belongs on a warehouse through the runner.

## Airflow deployments

```json
"finance-pipelines": {
  "kind": "airflow",
  "entrypoint": "airflow",
  "airflow": "2.10",
  "warehouse": "small"
}
```

`entrypoint` is a folder containing `dags/`. Airflow only **orchestrates**; its tasks run programs
on warehouses with `FDPRunOperator`:

```python
from fdp.airflow import FDPRunOperator

models = FDPRunOperator(task_id="build_models", program="finance-models",
                              args={"command": "build", "select": "+tag:daily"})
report = FDPRunOperator(task_id="sales_report", program="sales-report",
                              args={"end_date": "{{ ds }}"}, warehouse="large")
models >> report
```

| Parameter | Meaning |
|---|---|
| `program=` | A program from the **same repository at the same commit** as the Airflow deployment (deployed), or from your workspace (test deployment). |
| `workload=` | **Any deployed program** you may use, by its address name, so pipelines can combine programs and dbt projects from other teams' repositories. |
| `args=`, `warehouse=` | As in the run form; Airflow templates such as `{{ ds }}` work. |

Each task streams the warehouse run's console into the Airflow task log, fails when the run fails,
and stops the run when the task is cleared or killed.

**Testing:** run the program from the workspace runner (**Start Airflow test deployment**). Airflow
starts on a warehouse with the DAGs straight from your workspace, and **Open Airflow** opens its UI
(hub, or an editor tab in VS Code). Its tasks run your workspace's programs. An idle test deployment
suspends like an app.

**Deployed:** the UI is at `https://<fdp host>/p/<name>/` for everyone allowed (the gateway
handles sign-in; Airflow has no login of its own). Run history lives on the data volume and
survives redeploys. A new commit **recreates** the deployment (the old scheduler stops before the
new one starts), so DAGs never run twice.

### Standalone or distributed

```json
"airflow_settings": { "mode": "distributed", "workers": 3, "worker_concurrency": 8,
                      "worker_warehouse": "small", "parallelism": 32 }
```

| | `standalone` (default) | `distributed` |
|---|---|---|
| Containers | one | webserver, scheduler (+ triggerer), N Celery workers, a Valkey broker |
| Metadata | SQLite on the data volume (the layer's volume in a managed layer) | its **own database**: in a managed layer, a PostgreSQL pod of the deployment inside that layer; otherwise created automatically on the server admins configure (`FDP_AIRFLOW_METADATA_URL`) |
| Tasks at a time | 1 | `workers × worker_concurrency`, capped by `parallelism` |
| Used for | small pipelines, workspace test deployments | production pipelines with parallel tasks |

In distributed mode every component shares the Fernet key and a task-log folder, so the UI shows
logs from every worker. A new commit stops the old components before starting the new ones (never
two schedulers); the broker stays up. A missing component is restarted. Workspace test deployments
always run standalone.

In a managed layer every component, including the broker and the metadata database, runs in
that layer's namespace. The database keeps its data on the layer's volume, so redeploys and restarts
keep the DAG history. The broker requires a password, and the layer's network policies let only
Airflow components reach brokers and metadata databases. The database password, broker password
and Fernet key are generated once per deployment and kept in the secret store.

## Who runs it: identities

| What | Runs as |
|---|---|
| A workspace run (any kind) | You (your grants, your dev schema for dbt) |
| A deployed program or dbt project, run from its page | The person running it |
| A deployed app, Airflow deployment, or dbt docs job | The deployer, or the **service account** chosen under *Run as* |
| A task started by a deployed Airflow | The Airflow deployment's identity |

Use a [service account](deploying.md#service-accounts-and-secrets) for anything that should keep
running independently of a person.

## Versions

Pick versions per program (or per file) in `fdp.json`: `"python": "3.11"`, `"r": "4.3"`,
`"dbt_version": "1.7"`, `"airflow": "2.9"`. Admins choose which versions are offered (up to 5 per
runtime) under **Administration → Runtimes**. Images are built with
`make runtime KIND=python VERSION=3.11` (or come from your registry). Requesting a version that
isn't offered fails before anything starts, with the list of available ones.
