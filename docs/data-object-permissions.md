# Data object permissions

Schema and source grants are additive and include current and future descendants.
Use individual targets when a person or workload should access selected tables,
columns, files or directories. Remove broader grants first: a narrower child grant
does not cancel an inherited grant.

| Role | Physical object authority |
|---|---|
| Viewer | Discover metadata; no row or file contents |
| Executor | Read rows or file contents |
| Contributor | Write data; does not imply reading data |
| Manager | Edit descriptive metadata; no payload authority |
| Admin | Read, write and administer the object |
| Owner | Admin plus delegation |

Page Viewer remains separate. For example, discovering storage requires `pages` Viewer
on `storage`; reading a table requires its payload permission. Runtime identities use
their admitted object grants and layer boundary, rather than desktop page permissions.
Interactive group grants use only the selected group. Explicit principal grants remain
additive, including when the person switches groups.

## Terraform

`fdp_data_object` registers a permission target; it does not create or delete physical
data. A table belongs to a schema, a column to a table, and an S3 path component to a
namespace or directory. IDs are deterministic for the parent, kind and exact name.
Remove children before unregistering a target. Unregistering revokes its assignments.

```hcl
resource "fdp_data_object" "sales" {
  parent_id = fdp_schema.reporting.id
  kind      = "table"
  name      = "sales"
}

resource "fdp_data_object" "amount" {
  parent_id = fdp_data_object.sales.id
  kind      = "column"
  name      = "amount"
}

resource "fdp_role_assignment" "read_amount" {
  resource_id  = fdp_data_object.amount.id
  app_group_id = fdp_app_group.analysts.id
  role         = "Executor"
}
```

Use `fdp_permission_assignment` with individual registered actions for separate SELECT,
INSERT, UPDATE, DELETE, preview, metadata listing and file operations. Assignments must
match the target's server-derived layer. Protected classified data requires the existing
reviewed role/access-case or governed product flow; parallel named payload grants cannot
bypass that requirement.

## Enforcement and boundaries

SQL object grants use governed Trino admission. Every requested column and table is
checked, including sibling tables, metadata filtering and DDL. Native connection
credentials cannot safely enforce these boundaries and are refused for fine grants.
Masks, row filters, approved access expiry and product option restrictions still apply.
A data product containing one table grants that table, rather than its entire schema.
A product explicitly containing a schema intentionally grants that schema.

S3 object sessions receive narrowed STS policies. A file grant supplies no bucket listing
permission; directory listing is limited to its prefix. Create, overwrite, delete,
read and list are independently authorized. Raw write sessions require both create and
overwrite because S3 PutObject cannot distinguish them safely. Iceberg data buckets
remain accessible only through governed SQL, never file sessions.

```python
from fdp import files_object

text = files_object(file_permission_target_id).read_text("")
files = files_object(directory_permission_target_id, mode="list")
```

Run admission captures object authority in an immutable snapshot, alongside the existing
schema snapshot. Live interactive calls re-evaluate revocations and the selected group.
Already admitted jobs retain their captured data permissions. Customer database-native
permissions and existing broader FDP grants must also be reviewed before claiming that
an identity is restricted to a single object.


Named SQL reads require both `data.sql.select` and `data.queries.results.read` on
every selected table or column. The engine checks both at the referenced object;
a result grant on another source does not satisfy this check. Grant `data.queries`
Viewer for query status and Executor for the listed result/cancellation functions,
or use namespace `data` with an explicit action set. Legacy schema Reader/Executor
is an explicit bundle containing data reading and result retrieval.

Contributor alone supplies write actions through governed SQL. It does not supply
SELECT. Native write-only credentials are refused because the supported native
writer role also reads; grant both powers explicitly or use governed SQL.
