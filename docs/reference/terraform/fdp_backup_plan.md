# fdp_backup_plan

Generated from the current provider schema.

Independent control-plane, PostgreSQL, S3 or snapshot-aware Iceberg v1/v2 backup plan. Source export, destination use and scheduling are separately authorized. Destroy stops scheduling and retains archives and history.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `destination_id` | `basetypes.StringType` | Required |  |
| `enabled` | `basetypes.BoolType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `max_bytes` | `basetypes.Int64Type` | Optional, Computed |  |
| `max_objects` | `basetypes.Int64Type` | Optional, Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `next_run_at` | `basetypes.StringType` | Computed |  |
| `prefix` | `basetypes.StringType` | Optional, Computed |  |
| `retention_days` | `basetypes.Int64Type` | Optional, Computed |  |
| `schedule` | `basetypes.StringType` | Optional, Computed |  |
| `schemas` | `types.ListType[basetypes.StringType]` | Optional, Computed | PostgreSQL schemas or exact Iceberg namespaces; empty selects all. Iceberg includes every retained snapshot in each captured table metadata version. |
| `source_id` | `basetypes.StringType` | Optional, Computed |  |
| `source_kind` | `basetypes.StringType` | Optional, Computed |  |
| `source_prefix` | `basetypes.StringType` | Optional, Computed |  |
| `timezone` | `basetypes.StringType` | Optional, Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
