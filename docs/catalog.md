# Catalog, governance, service levels, and lineage

The catalog answers three questions: which data products exist that I could use, where does
a product I use come from, and what data does a dashboard use. It opens on **Data products**;
**All objects** is the full catalog for engineers. Both follow the layer chosen at the bottom
of the sidebar.

## Data products

A data product is what owners share: one or more catalog objects published together, for
example a dashboard with the tables it uses. It can hold tables and views, dashboards (apps),
APIs and Kafka topics, all in one layer. Everything else stays in the catalog as a lineage
object: it shows up in a product's lineage, but is not offered on its own.

- **Publish** from an object's own page (**Publish to catalog** on a dashboard, table, API or
  topic) or with **Publish a product** on Data products. Give it a name, a summary, a domain and
  a classification, and choose what is in it: starting from a dashboard, the tables it reads are
  already ticked, and you can add any other object you own. The first object leads and sets the
  card's icon; an object leads at most one product, but may be in several. You must own (the
  Owner role) everything you put in a product. Your active group becomes the product's owner.
- **Data products** shows products as cards to everyone in the layer, with or without access:
  name, summary, owner group, domain, what is in it ("Dashboard · 2 tables"), certification,
  when it was last updated, and where you stand: *You have access*, *Partly* (some of it),
  *Request access*, *Requested*. **Only what I can use** narrows it to products you can fully
  use. **My products** lists the ones your groups own.
- A **product page** has **Overview** (what it is, owner and steward, domain and
  classification, freshness and SLA, and what is in it with your access to each), **Lineage**
  (from the sources, left, to the objects in the product, right; steps you cannot open are
  dashed and show only their name, kind and owner), **Used by** (other products built on it)
  and, for owners, **Permissions**. Columns, previews and SQL stay on each object's own page,
  for people with access.

### Access options

A product can offer several ways to use it, each with its own **masks** (on table columns:
`hash`, `redact`, `partial`, `null` or an expression) and **row filters** (per table), for
example *Masked* (email hashed), *Own region* (rows of your region) and *Full*. One option is
the default. A request asks for an option; owners can move a consuming group to another option
or give a group access directly on the **Access options** tab.

The options' masks and filters apply to people who reach a table only through data products.
Someone who can read the table's schema directly is not limited by a product option. Reaching a
table through several options gives the least restrictive view: a column is masked only if every
option masks it, and rows are filtered only if every option filters them. The table's own masks
and filters (Masks and filters on the schema) always apply as well.

Each option shows the classification its consumers really see (see below): masked columns count
as their level's masked level, so a *Masked* option of personal data can be *internal* while
*Full* is *restricted*. Requesters see the options and their levels; which columns are masked is
shown to owners.

### Access through the product

Access is a role on the product itself. Groups with **Consumer** on a product may use
everything in it: Reader on its tables and topics (on a table's schema, so including the
schema's other tables), Executor on its dashboards, Consumer on its APIs. These roles are not
copied onto the objects; the platform derives them from the product. So:

- removing a group's Consumer role, removing an object from the product or unpublishing it
  ends that access everywhere, at once;
- adding an object to the product gives its consumers the use of it too;
- grants that groups have on the objects themselves are separate and stay.

**Owner** on the product also lets a group change it and decide requests. Owners manage both
roles on the product's **Permissions** tab, also without a request.

**Request access** asks the owners for access for one of your groups, with a reason. Owners
answer under **Access requests** (the sidebar counts what waits) and may add a note; approving
makes the group a Consumer. Requests, decisions and role changes are in the audit trail.
Requesters see the outcome under **My requests** and may withdraw a waiting request.

## Classification

Classifications are configurable under **Administration → Classifications** (or
`fdp_classifications` in Terraform): an ordered list from least to most sensitive, starting with
public, internal, confidential and restricted. Each level says what **masked** data of that level
counts as (itself or a lower level).

- **Columns** are classified and tagged (for example `pii`) on a table's Overview by its owners,
  or with `fdp_column_classification`.
- A **table or view** inherits the highest level of its columns. Owners can classify the object
  itself higher (Governance, or `fdp_catalog_governance` with `object_path`), never lower than its
  columns. A schema is the highest of its tables.
- For **access**, a column the group will see masked counts as its masked level; a level set on
  the object itself always counts. Access policies (forms and reviews per classification and
  layer) build on this.

## All objects

**All objects** searches registered data sources/schemas, known tables, deployed
apps/programs, and imported dbt/Airflow objects you may open. Files and folders are not
listed: Storage and Workloads show them.
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
