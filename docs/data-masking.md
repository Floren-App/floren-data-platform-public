# Column masks and row filters

Schema roles decide **which tables** someone may read. Column masks and row filters decide
**what they see in them**: a masked column shows a hidden or transformed value, and a row filter
limits the rows. Both are set per table and apply to everyone who queries the table, except the
groups and identities you exempt.

They are enforced by the layer's SQL engine (Trino), so the same rules apply wherever data is
read through the platform: the SQL editor, table previews, apps, programs, dbt, Airflow and the
HTTP API. A workload sees what its identity is allowed to see.

## Column masks

| Method | What people see | Column types |
|---|---|---|
| `null` | `NULL` | any |
| `redact` | `****` | text; other types show `NULL` |
| `hash` | SHA-256 of the value, as hex: equal values stay equal, so joins and counts still work | text; other types show `NULL` |
| `partial` | the last four characters, the rest as `*` (`************4242`) | text; other types show `NULL` |
| `expression` | your own Trino SQL expression over the table's columns | any; cast to the column's type |

Examples of expressions:

```sql
-- Only the year of a birth date
date_trunc('year', birth_date)
-- Salaries for HR, nothing for others
CASE WHEN is_member('HR') THEN salary END
```

## Row filters

A row filter is a condition; people see the rows for which it is true. One filter per table.

```sql
-- Everyone sees EU customers; the global analysts group sees all of them
region = 'EU' OR is_member('Global analysts')
```

`is_member('<group name>')` is true when the person (or the workload's identity) belongs to that
app group, directly or through a directory group. The platform resolves it before the engine
runs the query, so one filter can give each group different rows.

## Exemptions

Each mask or filter lists exempt **groups** and exempt **identities** (people or managed
identities). Exempt callers see the real values or all rows. Exempt a pipeline's managed identity
when it must process the real data, and keep people masked.

Platform administrators are not exempt by default: like everyone else they need an exemption,
and every change to a policy is audited.

## Who manages policies

Creating, changing or removing a policy requires the **Owner** role on the schema (or platform
administrator), because it decides who sees what. Everyone who may read the schema can see its
policies, so people know when a value is masked. Changes apply to queries that start afterwards;
running queries keep the policies they started with.

```sh
curl -X POST "$FDP_API/v1/data-policies" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
  "schema_id": "<schema id>", "table": "customers", "kind": "column_mask",
  "column": "email", "method": "partial", "exempt_groups": ["<app group id>"]
}'
curl -X POST "$FDP_API/v1/data-policies" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
  "schema_id": "<schema id>", "table": "customers", "kind": "row_filter",
  "expression": "region = '\''EU'\'' OR is_member('\''Global analysts'\'')"
}'
```

`GET /v1/data-policies?schema_id=...` lists them; `PUT` and `DELETE /v1/data-policies/{id}`
change or remove one (table, kind and column are fixed; create a new policy to change those).
Every change is recorded in the [audit trail](audit-and-siem.md) as `data_policy.create`,
`data_policy.update` or `data_policy.delete`, with the full policy.

## What cannot get around them

- **Views** must use `SECURITY INVOKER`, so a view reads its tables as the person querying it,
  masks and filters included.
- **Copies** made with `CREATE TABLE AS SELECT` or `INSERT ... SELECT` contain what the person
  could see: masked values and filtered rows.
- **Iceberg metadata tables** (`customers$files`, `customers$partitions` and the others) expose
  column bounds and partition values, so they are refused for a table with policies that apply
  to the caller.
- **Native database logins** (the legacy direct PostgreSQL connections) are refused for schemas
  with policies; those schemas are reachable only through governed SQL. Iceberg storage is
  already reachable only through SQL.

## Limits

- Expressions use the table's own columns and built-in functions; they cannot contain
  subqueries or refer to other tables. Expressions are stored normalized (comments removed).
- Conditions on identity attributes from the IdP (for example a person's region) are planned;
  use `is_member` with groups until then.
- Policies are managed through the HTTP API for now; OpenTofu resources and pages in the app
  follow.
