# Catalog, governance, service levels, and lineage

Open **Catalog** in the activity bar. Search registered data sources/schemas, known tables
and files, workload folders, deployed apps/programs, and imported dbt/Airflow objects.
Everything runs inside Floren Data Platform: its metadata database, API, UI, login, and IAM. There
is no DataHub deployment or separate metadata login. PostgreSQL uses a native GIN text
index; SQLite uses FTS5. The lineage explorer uses React Flow and Dagre for interaction
and layout.

Discovery follows the selected **Active group**, including inherited permissions. Search,
suggestions, facets and lineage do not combine other groups' objects into this view.
Select **Global Admin** to browse everything as a global administrator. Existing-object
actions still use primary and secondary authorization; browsing is a separate filter.

Search covers names, paths, descriptions, tags, column names/descriptions, owners,
repository and branch. Results rank names above columns/tags, then paths and other
metadata. Exact names rank first. Unknown spellings fall back to close matches when
there are no exact results. Autocomplete is keyboard accessible, and Ctrl+K / Cmd+K
opens and focuses catalog search. Every suggestion is filtered by current IAM.

Words combine with AND; use quotes for phrases, OR for alternatives, NOT or a minus
for exclusions, parentheses for grouping, and `*` for wildcards. Field expressions
include `name:`, `type:`, `domain:`, `owner:`, `tag:`, `column:`, `platform:`, `branch:`,
`path:`, `description:`, `status:` and `classification:`. Structured facet expressions
match exact values unless a wildcard is explicit, for example:

```
domain:Finance (type:table OR type:dbt) -status:deprecated
column:customer_id tag:monthly
owner:"Finance analysts" name:revenue*
```

Filters include type, domain, platform, owner group, tag, column, branch, governance,
classification and SLA. Sort by relevance, name, or most recently observed metadata.
Pagination, totals, and facets include only objects the current identity can read.

Each result has Overview, Lineage, Governance, SLA, and Access pages. **Open object**
returns to the original program/app/data page. **Access** edits the existing app-group
role assignments, using the original RBAC boundary. Tables/files use their schema's
IAM; there are no individual-object grants. Catalog metadata ownership
and certification never grant resource or data access.

Search queries the control-plane metadata index, not live data sources. Data objects are
indexed when their metadata is opened in the Data browser, or when native lineage resolves
them to a registered schema. No catalog search initiates a SQL/storage crawl or preview.
Discovery of a new schema still requires explicit schema registration. This preserves the
platform's on-open querying model; unvisited/unreported tables are not searchable yet.

## Governance

Governance records a description, domain, tags, accountable app group, steward/contact,
classification, and status: draft, reviewed, certified, deprecated. Contributors (data
contributors included) can edit documentation/domain/tags. Owners control accountability,
stewardship, classification, and certification. Certification requires a description,
domain, accountable group, steward, and classification. Material documentation edits
return a certified object to reviewed, requiring a fresh Owner certification.

Uncertified products remain deployable and available according to IAM. Classification is
metadata, not an automatic deny rule. Governance, SLA changes and imports are audited.

## SLAs

Owners configure maximum data age in minutes and, on deployed programs, a minimum
successful-run percentage over a rolling window (default seven days). Leaving targets
empty disables the checks. Status is healthy, breached, unknown, or not configured.
Missing observations are unknown rather than healthy. An independent background evaluator
also persists history and in-app alerts. Policies support early warnings, scheduled
deadlines/timezones, quality results, and optional explicit input gates on new workload
runs. See [KPIs, quality and SLAs](metrics-and-slas.md). External notification delivery
and app shutdowns are not configured.

Freshness comes from successful materialization/run observations, dbt source freshness
results, or completed Trino writes. Documentation generation, tests, metadata browsing
and deploying code do not refresh data freshness. Run success uses completed runs in
the selected window, counting failed/stopped runs against the target. Dataset-level
successful-run percentages are not inferred from every job that reads that dataset.

## Lineage evidence

Lineage collection is automatic for programs using Floren Data Platform's managed connections.
There is no required `lineage` section in `fdp.json`, and no opt-in flag:

| Access path | Automatically collected evidence |
| --- | --- |
| Python/R apps and programs querying through the shared SQL gateway | Tables read and written by successful queries, attributed to their workload/run |
| dbt projects | Native model/source dependencies, materialized datasets, tests and column metadata from artifacts |
| Airflow | Native DAG/task dependencies and the deployed programs each FDPRunOperator triggers |
| Managed API gateway | Successful API → calling program dependencies |
| Python/R `fdp.files` reads/writes | File inputs/outputs matched to registered namespaces by the SDK |

The resulting chain is API → ingestion → raw dataset → dbt models → output dataset → app.
Airflow's triggering relationships remain an optional orchestration overlay. IAM and layer
boundaries still apply; a grant or `data_sources` declaration alone does not prove data flow.

Runtime evidence appears after the relevant operation succeeds. Open each dashboard tab or
exercise each code path whose dependencies you want observed; an unexecuted branch cannot
be discovered from query history. dbt dependencies come from its artifacts, and Airflow's
task graph is collected as DAGs are parsed. Arbitrary direct database/S3/HTTP clients that
bypass the managed gateway and SDK are not automatically instrumented. Keep manual hints
only for those cases or for explicitly documenting dependencies before their first use.

The interactive explorer follows upstream dependencies and downstream impact, with
depth, direction, evidence and observation-time filters. Pan/zoom, minimap, fit-to-view,
graph search and per-node expansion make larger graphs navigable. Collapse removes
that expansion while retaining shared nodes from other expansions. Airflow task nodes
appear inside their DAG's pipeline box when both are present in the selected graph.
The initial view shows immediate neighbors; expand individual nodes or increase depth.
Select an entity to inspect ownership, governance, SLA and columns; center its lineage
or open its catalog page. Impact analysis lists reachable downstream entities and hop
counts within the selected graph. Column display shows metadata, not inferred column
transformations. Connection evidence lists relationship, origin, pinned revision/run/
query identifier, and observation time. Containment does not claim a data transformation.
The time filter restricts currently stored evidence by its observation time; it does not
reconstruct deleted edges or a historical snapshot.

Every node, both endpoints of every edge, and its producer are filtered through current
IAM before traversal. Hidden producers cannot bridge otherwise visible datasets.
Graphs are bounded to 150 nodes and 400 connections. This is object/table-level lineage;
dbt column descriptions/types are imported, but column-to-column transformations are not
inferred. There is no SQL text, row data, environment, or credential storage in the index.

Evidence sources:

- `program_declared`: program `data_sources` (coarse `uses`) and explicit inputs/outputs.
- `dbt_manifest`: native manifest dependencies, models, sources, tests, exposures,
  materializations; catalog column metadata; node execution/test status; source freshness.
- `airflow_dag`: native DAG/task structure, upstream task IDs, operator names, rendered
  inlets/outlets, and links to called workloads or same-repository programs.
- `program_runtime`: registered inputs/outputs reported by a running program. Output
  refresh observations are recorded only when the orchestrator confirms the run succeeded.
- `trino_runtime`: engine-authorized read/write relations promoted into lineage only
  after the query finishes successfully. Denied/failed queries do not produce these edges.

Native imports are idempotent snapshots per dbt program and Airflow DAG. Removed
dependencies are removed without replacing another producer's evidence. Physical objects
are linked only when their source/schema is registered; unresolved dbt objects remain
logical project nodes. Workload ingestion is restricted to its signed workload/run
context. A workload Contributor can also import native JSON from its Lineage page.
Imports describe dependencies; they never provision identities, schemas, or grants.

### dbt

Deployed dbt runs upload `manifest.json`, `catalog.json`, `run_results.json` and
`sources.json` when available, including dbt programs invoked from a deployed Airflow
deployment at its pinned commit. Run results are preserved before docs generation.
The control plane also imports available completed dbt artifacts from its shared run
volume, so runs that could not upload their artifacts still populate the catalog. Each search checks
up to the latest 100 completed deployed dbt runs for unprocessed artifacts.

Artifacts follow dbt's [manifest](https://docs.getdbt.com/reference/artifacts/manifest-json),
[catalog](https://docs.getdbt.com/reference/artifacts/catalog-json),
[run results](https://docs.getdbt.com/reference/artifacts/run-results-json), and
[source freshness](https://docs.getdbt.com/reference/artifacts/sources-json) formats.

### Airflow

The Airflow runtime configures `fdp.airflow_lineage.FDPLineageBackend` automatically. FDPRunOperator also reports its DAG before submitting a task. The backend
collects rendered native inlets/outlets after operator execution. The scheduler also
syncs changed serialized DAG metadata every 30 seconds, so dependencies appear before
a DAG is executed, without parsing DAG code in the control plane. It follows Airflow's
[lineage backend contract](https://airflow.apache.org/docs/apache-airflow/2.10.2/administration-and-deployment/lineage.html).

Dataset/Asset identifiers such as `Dataset("fdp://lake/finance/sales")` resolve to
the registered source `lake`, schema `finance`, object `sales`. Native Table entities can
also identify source/schema/name through cluster/database/name. External URLs are never
fetched, and URIs with credentials/query tokens are excluded. Other URI schemes are
unresolved. Native metadata appears as DAGs are parsed by Airflow or executed/reported. FDP
does not poll external Airflow servers or execute DAG code in the control plane.

### Python/R programs

Ordinary Python/R programs need no lineage configuration. For example, using
`fdp.query("warehouse", "SELECT * FROM reporting.revenue")` in a deployed
app automatically adds `revenue → app` after the query completes. The same applies
to SQL issued through `fdp.connect` or the R SDK's managed connection.

Files work the same way. In a deployed job, reads and writes through `fdp.files()`
(Python) or `files()` (R) are mapped to the granted S3 namespace they fall under and
reported when the program exits. For example, `put("finance/2026/sales.csv")` records an
output to that file. Workspaces record nothing; only deployed runs produce lineage.

For an external connector that bypasses Floren Data Platform, optionally declare known dependencies
in `fdp.json` (the same format works for R programs):

```json
{
  "programs": {
    "daily-report": {
      "entrypoint": "report.py",
      "lineage": {
        "inputs": [{"source": "lake", "schema": "finance", "name": "sales"}],
        "outputs": [{"source": "finance-dw", "schema": "analytics", "name": "daily_report"}]
      }
    }
  }
}
```

Python jobs can instead call `fdp.lineage.report(inputs=[...], outputs=[...])`
with those identifiers when their external operation completes. These are supplementary
declarations, not prerequisites for automatic SQL, API, dbt, Airflow or file evidence.

## API

`GET /v1/catalog/search`, `GET /suggest`, `GET /assets/{id}`, `GET /assets/{id}/lineage`,
`PUT /assets/{id}/governance`, `PUT /assets/{id}/sla`,
`POST /resources/{resource_id}/import/dbt|airflow`, and execution-only
`POST /ingest/dbt|airflow|program` are under `/v1/catalog`. The catalog's Access tab uses
the existing `/v1/rbac/resources/{resource_id}/assignments` endpoints.

## Focused upstream lineage

Opening an object's Lineage tab starts with **Upstream lineage**. It follows actual data
dependencies into the inspected object: managed API → ingestion program → input dataset →
dbt models → output dataset → app. Only visible, authorized dependencies are included;
permissions alone never imply a data relationship.

Within a dbt project, manifest model dependencies replace duplicate paths through every
intermediate physical table. Input/output dataset boundaries remain visible, as do model
names. **Upstream & downstream** adds only actual descendants of the inspected object.
Upstream and downstream walks stay separate: shared inputs do not pull in sibling apps or
unrelated branches. Tests and connection-level source usage stay outside the data-flow view.
Direction, depth, evidence, time and impact controls remain available. Native metadata is retained.

**Show orchestration** is off by default. It overlays authorized Airflow DAGs and only the
tasks that trigger programs or dbt projects on the selected data paths. Dashed **triggers**
arrows distinguish scheduling from data movement. Relevant tasks sit inside their DAG;
selected dbt models sit inside their triggered project. Other tasks, models and tests are
not expanded. The overlay cannot widen the data dependency traversal, and impact analysis
ignores control edges. Evidence/time filters and IAM apply before overlay selection.
Duplicate connection observations appear as one connection with their combined evidence.

The initial zoom keeps the inspected object and its inputs readable. Stage shortcuts jump
to APIs, ingestion, input datasets, dbt models and output datasets; **Fit entire chain** shows
the whole upstream flow. The default upstream depth is sixteen connections, expandable to
thirty-two or incrementally from a node. Node/edge limits and IAM filtering still apply.

Successful managed API requests automatically record API → calling program lineage. Request
URLs, query values, payloads and credentials are not indexed. Historical successful calls by
a workload's unique managed identity can be restored from audit events with
`catalog.harvest_api_calls(db)`; human and shared-service-account audit events are not guessed
into producer relationships. Live run/workload context also supports authorized service
accounts, because its producer is explicit. Restoring audit evidence does not rerun data jobs.
