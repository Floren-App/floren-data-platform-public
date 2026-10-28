# fdp_query_log_settings

Generated from the current provider schema.

The query log of every query on the layers' Trino (who or what ran it, tables, duration, result); declare once. Destroy restores the defaults (90 days, no SQL text). Import with ID "query_log".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `record_sql` | `basetypes.BoolType` | Optional, Computed | Keep each query's SQL text. Off by default: queries can contain literal values such as personal data. |
| `retention_days` | `basetypes.Int64Type` | Optional, Computed | Rows older than this are removed. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
