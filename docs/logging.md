# Workload logs

The workload log shows what happens in your workloads in one place, with the time, level and code
version of every line. You see it in four places:

- a workload's **Logs** tab;
- the **Log** section of a run;
- the **Logs** tab of every Workloads folder (its workloads and subfolders);
- **Workloads → Logs** in the sidebar: every workload you may read, in your layer.

Filter by level (all, info and up, warnings and errors, errors only), search messages, and turn
**Live** off to stop refreshing. Open **details** on a line for its structured fields.

## What the platform records

These lines are written automatically, whichever way the work was started:

| Event | Level | When |
|---|---|---|
| `run.started` | info | A run (on demand, from Airflow, from a workspace) starts |
| `run.queued` | info | A run waits for warehouse capacity |
| `run.succeeded` | info | A run finishes, with its duration |
| `run.failed` | error | A run fails, with its duration and the reason when known |
| `run.stopped` | warning | Someone stops a run |
| `workload.deployed`, `workload.update_found` | info | A commit is deployed or found on the branch |
| `workload.deploy_failed`, `workload.sync_failed` | error | A deploy fails, or the branch cannot be read |
| `dag.started`, `dag.succeeded` | info | A DAG run of a deployed Airflow starts or succeeds |
| `dag.failed`, `task.failed` | error | A DAG run or one of its tasks fails |

Airflow lines come from a listener in the Floren Data Platform SDK, installed in the platform's
Airflow image. It only reports in deployments, never from a workspace's own Airflow.

## Logging from your code

Python:

```python
import fdp.log

fdp.log.info("loaded rows", table="sales", rows=120)
fdp.log.warning("3 rows without a region")
try:
    upload()
except Exception:
    fdp.log.exception("upload failed", attempt=2)   # adds the traceback
```

To send Python's standard `logging` there as well:

```python
import logging
logging.getLogger().addHandler(fdp.log.handler())   # INFO and up
```

R:

```r
fdp::log_info("loaded rows", table = "sales", rows = 120)
fdp::log_warning("3 rows without a region")
fdp::log_error("upload failed", attempt = 2)
```

Every line also prints to the run's console. Python sends lines in the background, in batches; a
logging outage never fails your program. Outside Floren Data Platform the lines only print.

## What each line carries

The platform, not your code, stamps each line with:

- **Time**: when it happened (your clock, if it is within a day of the platform's).
- **Run, workload and workspace**: taken from the run's own token, so code cannot log for another
  run or workload.
- **Version**: the commit the run or deployment came from; workspace runs have none.
- **Layer**: the layer of the workload or workspace.

Messages are limited to 4000 characters and structured fields to 4 KB. A run may write 10,000
lines; an app or Airflow deployment 5,000 lines per hour. Beyond that, lines are dropped and one
warning says so. Lines are kept for 30 days.

## Who sees what

- Lines of a run are visible to whoever may see that run. Workspace runs are private to their owner.
  Runs of deployed programs are visible to the person who started them, and to people who may use
  the service account a run ran as (for example Airflow's).
- Workload lines without a run (deploys, Airflow DAG runs, app lines) are visible to everyone who may
  read the workload.
- Administrators see everything.

The log does not grant access to anything. It is separate from the [audit trail](audit-and-siem.md),
which records who did what, and from the query log in Administration.
