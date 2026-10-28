# Iceberg tables and files

PostgreSQL and Iceberg tables use the shared Trino SQL endpoint and the same schema
RBAC. Plain files use a separate S3 prefix API. Table catalog/storage credentials
stay on infrastructure; workloads never receive lake keys.

## Table access

Admins configure an Iceberg REST/S3 catalog in Trino and add an **Iceberg tables on S3**
storage integration with its catalog name and warehouse location, for example
`{"catalog":"lake","warehouse":"s3://lake/","catalog_uri":"http://iceberg-rest:8181"}`.
Open the source under **Data** to register namespaces, then open a namespace to set
up access. Assign app groups the fixed Reader, Contributor, Admin or Owner roles on
its **Permissions** tab. Namespace creation requires an existing
registered grant; grants cover future tables as well as current ones.

```python
import fdp

engine = fdp.connect("lake", schema="finance")
with engine.connect() as con:
    rows = con.exec_driver_sql("SELECT * FROM lake.finance.sales LIMIT 10").fetchall()
```

```r
con <- fdp::connect("lake", schema = "finance")
sales <- DBI::dbGetQuery(con, "SELECT * FROM lake.finance.sales LIMIT 10")
DBI::dbDisconnect(con)
```

An Admin can create/replace tables and load across catalogs:

```sql
CREATE SCHEMA IF NOT EXISTS lake.finance;
CREATE TABLE lake.finance.sales AS SELECT * FROM finance.public.sales;
```

Reader can SELECT. Contributor can write rows but cannot create, alter or drop
tables. dbt builds require Admin because materializations create/replace objects;
use dbt-trino for both PostgreSQL and Iceberg. Use views with explicit
`SECURITY INVOKER`. Native passthrough, custom storage locations and unreviewed
maintenance procedures are denied.

Shared caller-mode apps supply the signed viewer header on each data request:

```python
result = fdp.query("lake", "SELECT * FROM finance.sales WHERE id = ?",
                         [42], viewer_token=viewer)
```

The unqualified name above is the `finance.sales` schema/table in the selected
`lake` catalog. Fully qualified names work across all catalogs authorized to the
viewer. Keep tokens and results local to that viewer; never cache shared results
without the authorization context. Fixed apps use their registered service or an
authorized service account and the ordinary connection API.

Interactive SQL against both catalogs is available in a [SQL workspace](sql-workspace.md).

## Plain files

Add an **S3 files** storage integration with bucket, region and role_arn (optionally
endpoint_url, sts_endpoint_url and a base prefix), plus server-only provisioning
credentials for STS. Register logical schema resources
with their relative prefixes, provision them and assign app groups. Readers list/
read files; Contributors also write/delete. IAM policy management stays server-side.
The S3 bucket containing Iceberg tables cannot be registered as a plain-file source.

```python
files = fdp.files("lake-files")
files.write_json("summaries/today.json", {"sales": 42})
summary = files.read_json("summaries/today.json")
```

```r
files <- fdp::files("lake-files")
summary <- files$read_json("summaries/today.json")
```

Use `viewer_token=viewer` for delegated plain-file requests in shared caller apps.
Delegated file reads/writes are limited to 5 MB and listings to 1,000 entries.
Recreate direct STS clients after credential expiry.

## Local deployment

Compose's example external storage (`deploy/compose/example-storage`) includes RustFS
and a private Iceberg REST reference catalog in front of Trino 484. `make storage-example`
registers it as the `lake` and `lake-files` storage integrations (see
[storage integrations](storage-integrations.md)); `EXAMPLE_STORAGE=1 POKEMON_STORAGE=0 make seed` then registers namespaces, creates them through governed SQL and assigns
example roles. `load-lake` copies warehouse sales into Iceberg with a cross-catalog
SQL operation; `lake-models` builds dbt tables through the same engine. Table data
stays in the lake bucket and plain-file summaries in the files bucket.

Direct PyIceberg catalogs and root storage keys are not issued for governed tables.
SQL and dbt models use Trino; file helpers continue to support scoped plain-file access.
Production may replace the development REST fixture with a durable compatible
catalog. See [shared SQL deployment](query-execution.md) and [RBAC](rbac.md).
