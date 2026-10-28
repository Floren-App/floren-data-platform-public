# KPIs, data quality and service levels

Open a catalog object and select **KPIs & quality**. Its Owner can create reusable
business KPIs or data quality definitions with a description, unit, calculation,
dependencies, and warning/failure limits. Definitions and observations use the owning
object's IAM; additional dependencies must be readable too. Test/Prod definitions,
queries, observations and input gates stay in their respective layers.

## Measure and inspect

- **Measure** executes a saved read-only SQL query through the shared Trino gateway,
  using the caller's data permissions, narrowed to declared dependency schemas.
  The query must return exactly one row; its first column is a finite numeric value.
- A definition without SQL can receive numeric observations from a Contributor or
  managed identity through the SDK/API. Status is computed server-side from its limits.
- Definitions retain immutable version snapshots. Observations retain definition version,
  unit, threshold snapshot, timestamp, origin, job and code revision when available.
- Measurement history includes a small trend chart. Changing a definition does not
  rewrite historical values. A new definition version needs a new measurement before
  its quality check can satisfy an SLA.
- Disable a definition to retain its history while excluding it from quality evaluation.
- KPIs and quality checks appear in catalog search and lineage with `measures` edges.

Example row-count calculation on a data product:

```sql
SELECT count(*) AS value FROM warehouse.reporting.revenue
```

For completeness, calculate a percentage in a dbt model or SQL definition and test the
acceptable range. Recording the actual percentage is more useful than a pass/fail alone.
Definition Owners can use both failure bounds and tighter warning bounds.

## Automatic dbt checks

Floren Data Platform imports test status and failing-row counts from native `run_results.json`
alongside the manifest, execution time, revision and invocation metadata. Importing the
same execution twice does not duplicate measurements. Removed tests are disabled rather
than deleting their history. Generic and singular tests use the same result path.

Dataset-level health assertions require the producer's data-write authority, checked
against its immutable run admission snapshot for jobs. Descriptive lineage imports without
that authority still show the project's tests, but cannot repair another dataset's SLA.
Read-only testing can instead use an Owner-defined SQL quality metric with governed
measurement, or retain the results on the producing dbt project.

The independent evaluator backfills completed dbt artifacts already captured by FDP.
It never reruns dbt. Where an upload is incomplete, failing-row values come from the
captured raw artifacts.

The dbt program's command picker includes **freshness**, which runs `dbt source freshness`
and imports `sources.json`. This is a source check, not a data materialization.

## SLA policies

The catalog object's **SLA** section supports:

- Maximum age, with an early warning at a configurable percentage of the age budget.
- Daily deadlines, selected weekdays, IANA timezone, warning window and grace period.
- On-time compliance over the configured window, including periods with no run at all.
- Required passing quality checks and maximum acceptable quality-result age.
- Existing completed-run success percentage for deployed workloads.

Status is **Healthy**, **At risk**, **Breached**, **Unknown**, or **Not configured**.
Missing evidence is never treated as passing. Monitoring starts when the policy is saved;
historical deadlines before that point are not invented. Changing the deadline calendar
starts a new calendar monitoring period. Deadline calculations use the policy's timezone,
including daylight saving changes, rather than the server's local clock.

Choose the freshness evidence deliberately:

| Evidence | Meaning |
| --- | --- |
| Successful materialization | The dataset was successfully produced/refreshed |
| Successful source check | An ingestion verified the source, even if nothing changed |
| Source data timestamp | The upstream content's own latest timestamp |

Tests, docs generation, deploying and browsing do not refresh dataset evidence. An API
with unchanged content can be healthy under a source-check policy while its data watermark
remains old. A failed API request must not report a successful source check.

The control-plane evaluator runs every 60 seconds by default, independently of Airflow
(`FDP_SLA_EVALUATION_INTERVAL_SECONDS`). It persists status/period changes and opens
in-app alerts, visible in Catalog and on the object's SLA page. Owners can acknowledge
alerts. Acknowledgement does not resolve a breach; passing evidence resolves it.
No email, Slack or external notification service is configured by this feature.

## Explicit enforcement

A deployed workload Owner can configure upstream catalog object IDs and select
**Block new runs on breached inputs**. Unknown/unconfigured required inputs also block
by default. The gate checks only those explicit dependencies, their layer, and the actual
execution identity's access, at new-run admission, including Airflow-triggered jobs.

The error links to the affected catalog SLA pages. Producers can still repair their data;
the consumer gate does not stop running jobs, revoke access, or shut down apps.
Deployment/promotion gates and request blocking are not part of this mechanism.

## Python SDK and HTTP API

```python
import fdp

# In ingestion, after a successful source check (requires Contributor on the dataset):
fdp.observe_dataset(
    "catalog-asset-id", kind="checked", idempotency_key="ingestion-batch-id"
)

# Compute an Owner-defined SQL KPI with the current managed identity:
sample = fdp.measure_metric("catalog-asset-id", "metric-id")

# Or report a measured value with its numerator/denominator:
fdp.report_metric(
    "catalog-asset-id", "metric-id", 98.7,
    numerator=987, denominator=1000, idempotency_key="ingestion-batch-id"
)
```

Caller-mode apps must pass `viewer_token=` when measuring SQL on behalf of a viewer.
The helper uses the existing execution token/renewal; there are no embedded storage
credentials. Other clients can use the same authenticated HTTP endpoints.

Routes under `/v1/catalog`:

- `GET/POST /assets/{id}/metrics` — list or upsert definitions by stable key.
- `GET /assets/{id}/metrics/{metric}/versions` — definition revisions.
- `POST /assets/{id}/metrics/{metric}/measure` — governed Trino measurement.
- `POST /assets/{id}/metrics/{metric}/observations` — numeric report.
- `GET /assets/{id}/metrics/{metric}/history` — result history.
- `POST /assets/{id}/observations` — refresh/check/watermark evidence.
- `PUT /assets/{id}/sla`, `POST /assets/{id}/evaluate-sla`, `GET /assets/{id}/sla-history`.
- `GET /alerts`, `POST /alerts/{id}/acknowledge` — authorized in-app incidents.

Global Admin can query these metadata tables through `fdp_metadata.public`:
`metric_definitions`, `metric_definition_revisions`, `metric_observations`,
`dataset_observations`, `sla_evaluations`, and `sla_incidents`. Operational history stays
read-only through the metadata SQL catalog; definitions/policies use validated APIs.
