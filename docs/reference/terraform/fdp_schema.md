# fdp_schema

Generated from the current provider schema.

A governed schema (SQL) or namespace (S3 prefix) of a data source. Destroying it stops governing it; the data itself is never touched. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `append_only` | `basetypes.BoolType` | Optional, Computed | Trino SQL schemas: permit inserts and initial table creation, refuse destructive writes/DDL. Changing this contract requires re-registration. |
| `data_source` | `basetypes.StringType` | Required | Data source name. |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `prefix` | `basetypes.StringType` | Optional | S3 files only. |
| `provision` | `basetypes.BoolType` | Optional, Computed | Set up access in the source after registering (roles/policies). |
| `state` | `basetypes.StringType` | Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
