# Example: an end-to-end data product

This walkthrough builds a complete data product from public Pokémon trading-card data,
from an external API to analyst dashboards. It describes the reusable example in
[examples/pokemon](../examples/pokemon) and
[terraform/modules/pokemon](../terraform/modules/pokemon). It uses a few English
TCGDex sets, immutable batch files, raw records, dbt history/products, Airflow and
two Streamlit dashboards. Your installation and workload Git repository remain
separate from the public platform source.

## Data flow

```text
TCGDex managed API
       │ extract Python job
       ▼
RustFS landing: immutable JSON batches + manifests/checksums
       │ load Python job; one row per list record
       ▼
Raw PostgreSQL: append-only payloads + provenance / load commits
       │ dbt engineering models and tests
       ▼
SCD2 history + process-history evidence
       │ dbt product models and tests
       ├──> Reporting tables ──> Analyst explorer dashboard
       └──────────────────────> Engineering monitoring dashboard

Airflow orchestrates the jobs and supplies task/run relationships.
```

S3 batches hold lists, not one file per card. Raw loading preserves payloads as
faithfully as possible and adds record IDs, file/object identifiers, checksums,
batch/load IDs and timestamps. Transformations preserve the provenance needed to
trace a product row back through history/raw observations to the source file.

## Prepare infrastructure and source

1. Install FDP and configure the [provider mirror](terraform.md).
2. Declare isolated Prod and Non-Prod layers, PostgreSQL, RustFS and compute pools.
3. Configure distinct landing buckets/prefixes per layer, separate from Iceberg storage.
4. Create engineers and analysts app groups and their directory bindings.
5. Copy `examples/pokemon/` to the root of your own Git repository so `fdp.json`
   is at its root. Create `test` and `main` branches; configure promotion protection.
6. In an independent workload Terraform root, use the Pokemon module once per
   environment. See its [input variables](../terraform/modules/pokemon/variables.tf).

Each module receives the layer ID, source integration name, product schema ID,
engineer/analyst group IDs, private repository URL and existing compute pool names.
Supply repository credentials ephemerally when registering private Git access;
do not commit them. Pin remote module source to a reviewed platform commit.

## Identity and access

| Actor | Intended access |
|---|---|
| Extract identity | Registered TCGDex API and its own landing storage |
| Load identity | Landing reads and append-only raw writes |
| dbt identity | Raw reads and required history/product DDL/DML |
| Orchestrator identity | Execute only its own environment's pipeline steps |
| Analyst dashboard identity | Read reporting output only |
| Engineering dashboard identity | Read raw/history/process evidence |
| Analysts | The *Pokemon cards* data product with its Standard option (illustrators masked); no raw/history access |
| Engineers | Development and monitoring permissions as explicitly assigned |

Use each workload's built-in identity and `managed_identity_grants`. Test/Prod
must have independent workloads and grants, even if they share the source repo.
An analyst may view a Prod dashboard without receiving permission to modify Prod
or use a Prod execution identity from a Test workspace.

### Masks and filters (demonstration)

Set `demo_data_policies = true` on an environment to add [column masks and a row
filter](data-masking.md) to `reporting.cards_current`: analysts and the explorer
dashboard see prices rounded to whole euros, only the last four characters of the
illustrator and no rare cards. Data engineers and the `bootstrap` and `models`
identities that build the product are exempt, so the pipeline keeps processing real
data. Compare the explorer dashboard with the same query run by an engineer.

### Classification, the data product and access policies

The module classifies the columns that matter (levels from the platform's
`fdp_classifications`, see the `levels` variable): the illustrator is personal data
(`confidential`, tag `pii`) in `cards_current`, `dim_cards_history` and the raw
`records` payload; the market price is `internal`. Tables follow their most
sensitive column.

It publishes the **Pokemon cards** [data product](catalog.md#data-products)
(`cards_current`, `set_summary` and the explorer dashboard), owned by the data
engineers, with two access options:

- **Standard** (default) hides most of the illustrator's name, so it counts as
  `internal`. Analysts use the product with this option.
- **With illustrators** shows everything and counts as `confidential`.

With an [access policy](access-policies.md) on confidential data in Prod, a group
that asks for *With illustrators* there fills in the compliance form; the data
engineers (the product's owners) and then the privacy reviewers decide. The data
engineers own the lakes, so their own access needs no form.

## Apply and run

Apply platform configuration first, then the independent Pokemon root. Inspect
each plan and check a second plan is clean. The module declares registration,
permissions, governance and workloads; it is not a guarantee that a scheduled
ingestion has already populated the tables.

Inspect the published bootstrap and pipeline definitions before running them.
Bootstrap creates the required schemas/tables; run the Test Airflow DAG, inspect
each ingestion/dbt task and verify both Test dashboards. Keep Prod scheduling
paused until the deployment and access checks are complete. Promote through Git,
then initialize/run Prod deliberately with its own identities.

## Acceptance checks

- Re-running a committed load does not duplicate the same file's records.
- SCD2 has at most one current version per business key; changes preserve history.
- Current products retain record/file traceability and have passing dbt tests.
- Engineers can trace failures through process history and orchestrator runs.
- Analysts can read the explorer/output, and are denied raw/history access.
- Test identities cannot read, write or execute Prod resources, and vice versa.
- Catalog objects have the Pokemon tag, accountable ownership, visible layer and
  appropriate freshness/run SLAs. Uncertified status remains visible.
- App upstream lineage shows API → extract → landing/raw → dbt → product → app.
  The orchestration toggle adds Airflow relationships without treating every task
  trigger as a data transformation.

These are repeatable acceptance criteria. Do not infer success merely from green
publication status; check actual runs, data and denied-access cases.
