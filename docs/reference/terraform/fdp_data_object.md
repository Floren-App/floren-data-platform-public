# fdp_data_object

Generated from the current provider schema.

A stable permission target for a table, column, S3 file or directory. Metadata registration grants no data access. Destroy unregisters this target and revokes its assignments; it never deletes physical data. Children must be removed first. Import with the object ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `kind` | `basetypes.StringType` | Required | table, column, file or directory. |
| `name` | `basetypes.StringType` | Required | Exact SQL identifier or one file/directory path component. No path traversal or wildcard characters. |
| `parent_id` | `basetypes.StringType` | Required | Schema ID for a table or top-level file/directory; table ID for a column; directory ID for a nested file/directory. |
| `source_id` | `basetypes.StringType` | Computed | Server-derived data source ID. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
