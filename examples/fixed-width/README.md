# Fixed-width ingestion and seed-driven dbt parser

A line-ingestion script and one dbt project, targeting Limoon's governed Trino
path (for example an Iceberg source).

```text
text file -> raw_lines (complete physical lines + metadata)
file_definition.csv -> dbt seed -> file_definition table
raw_lines + file_definition -> dbt run -> parsed_customers
```

The layout seed determines positions, lengths and output column names. Parsing
uses `substr`, followed by `trim` and `nullif(..., '')`. All columns in the parsed
table, including its metadata, are VARCHAR. IDs and amounts therefore
retain leading zeros. A later model can perform business typing.

## Sample layout

`parser/seeds/file_definition.csv`:

```csv
definition_name,column_name,start_position,field_length
customers_v1,customer_id,1,6
customers_v1,customer_name,7,20
customers_v1,country_code,27,2
customers_v1,balance,29,10
```

The sample file has three 38-character records. Its last record has a blank
balance field, including ten trailing spaces; those spaces are intentional.
The raw loader removes only a record terminator (CRLF/LF/CR). It does not trim,
split fields, discard blank lines or interpret values.

Positions are **1-based Unicode character positions**, matching Trino `substr`.
If the real specification uses byte offsets, especially with a multibyte encoding,
adapt the approach before using it. `--encoding` controls strict decoding; it does
not turn character offsets into byte offsets. This example has no header/footer
records or mixed record types.

## Raw table

The loader creates `raw_lines` in an existing registered schema:

```sql
create table if not exists lake.fixed_width.raw_lines (
    load_id varchar,
    definition_name varchar,
    source_file varchar,
    line_number bigint,
    record_length integer,
    ingested_at varchar,
    raw_line varchar
);
```

Each invocation appends a new load ID, even for the same file. Pass `--load-id`
(an orchestrator's run ID, for example) to make a retry safe: a load already
complete in the table is not appended again, and an interrupted one stops the run
until its rows are removed. This is a small batching example, not an exactly-once
production loader. For the real ingestion job, add a
file/load manifest, completed-load publication and the desired deduplication rule.
Keep versioned layout names immutable rather than changing how old rows parse.

## Run locally or from a workspace terminal

Prerequisites: a registered SQL integration, its actual Trino catalog name, an
existing registered `fixed_width` schema and the appropriate input read/target
create-write permissions. The default example uses source/catalog `lake`. No
schemas, credentials or grants are provisioned by this example.

For terminal dbt commands, use a dbt Core environment with dbt-trino; alternatively
use the included Limoon dbt programs below.

From this directory, inspect raw ingestion without a connection:

```sh
python ingest.py data/customers.txt --definition-name customers_v1 --dry-run
```

Then load the records through the existing FDP SDK and credential:

```sh
python ingest.py data/customers.txt --definition-name customers_v1 \
  --source lake --catalog lake --schema fixed_width
```

Create the existing platform dbt profile (no native source credentials):

```sh
fdp dbt-profile --source lake --schema fixed_width --write
```

Run these steps **in order**, using the same profile/catalog/schema:

```sh
dbt seed --project-dir parser
dbt test --project-dir parser --select raw_record_length --vars '{definition_name: customers_v1}'
dbt run --project-dir parser --select parsed_customers --vars '{definition_name: customers_v1}'
```

The layout is a seed in the same project, read through `ref('file_definition')`,
so dbt knows the parser depends on it. The macro queries the seeded table while the
model compiles, which dbt does just before running it; the seed must have been
loaded once before `dbt compile` or `dbt docs generate`. The length test rejects
short/long records and an unknown selected layout; run it as a gate before parsing
rather than silently dropping malformed rows.

The raw table defaults to the parser's target schema. If it differs, provide
`raw_schema` via dbt vars. The parser has no layout of its own: pass
`definition_name` (for example `customers_v1`) to every model and test run, which
fails without it. Add another layout as new rows in the seed under a new name.

## Run through Limoon

Copy this example to `/workspace/fixed-width` (or the workspace root), so its
`fdp.json` is discovered at the root/direct-child level. For a deeper checkout,
add appropriately prefixed program entries to your workspace configuration.

The included `fdp.json` provides the ingest and parser programs on `xsmall`. The
dbt program explicitly uses the same demo schema in development and deployment.
Edit the source/schema/warehouse for the installation before use.

Programs in a child folder are prefixed in the runner: for example
`fixed-width/fixed-width-ingest`.

1. Run **fixed-width-ingest** for the bundled sample, with file definition **customers_v1**.
2. Run **fixed-width-parse**, choosing command **seed**.
3. Run **fixed-width-parse**, choosing command **test**, select **raw_record_length**, vars
   **{definition_name: customers_v1}**.
4. After that succeeds, run **fixed-width-parse**, command **run**, select **parsed_customers**,
   with the same vars.

The script program loads the bundled sample by default; the terminal CLI accepts
another file path. The existing execution identity still needs the appropriate
source/schema permissions and runner grants.

## Generated parser query

`parser/models/parsed_customers.sql` calls:

```sql
{{ generate_parser(definition_name()) }}
```

The macro reads the seeded table with `run_query` at compile time and emits one
expression per field, in start-position order. There is no SQL data type in the
seed: `substr`, `trim` and `nullif` return VARCHAR, so every field is VARCHAR. It
generates the following business expressions:

```sql
nullif(trim(substr(raw_line, 1, 6)), '') as "customer_id",
nullif(trim(substr(raw_line, 7, 20)), '') as "customer_name",
nullif(trim(substr(raw_line, 27, 2)), '') as "country_code",
nullif(trim(substr(raw_line, 29, 10)), '') as "balance"
```

The layout is trusted project data: the macro does not validate it. A layout name
with no rows produces invalid SQL, so the run fails rather than parsing nothing.

The model also keeps `_load_id`, `_source_file`, `_line_number`, `_record_length`,
`_ingested_at` and `_definition_name` as VARCHAR for traceability. The raw table
retains the full padded input. Remove `trim`/`nullif` from the macro if field padding
or empty-string values themselves must remain significant downstream.

| customer_id | customer_name | country_code | balance |
|---|---|---|---|
| 000001 | Ada Lovelace | NL | 0000012500 |
| 000002 | Grace Hopper | BE | 0000000750 |
| 000003 | No Balance | NL | NULL |

References: [dbt run_query](https://docs.getdbt.com/reference/dbt-jinja-functions/run_query),
[dbt execute](https://docs.getdbt.com/reference/dbt-jinja-functions/execute),
[seed column types](https://docs.getdbt.com/reference/resource-configs/column_types),
[Trino string functions](https://trino.io/docs/current/functions/string.html).
