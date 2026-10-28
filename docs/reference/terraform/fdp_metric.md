# fdp_metric

Generated from the current provider schema.

A KPI or quality metric on a data product, optionally measured by a read-only SQL query. Every change creates a new version. Destroy disables it (history is kept). Import as <resource_id>/<key>.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `dependencies` | `types.ListType[basetypes.StringType]` | Optional, Computed | Catalog asset IDs the measure reads. |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `enabled` | `basetypes.BoolType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `key` | `basetypes.StringType` | Required |  |
| `kind` | `basetypes.StringType` | Optional, Computed | kpi or quality. |
| `maximum` | `basetypes.Float64Type` | Optional | Failure above this value. |
| `minimum` | `basetypes.Float64Type` | Optional | Failure below this value. |
| `name` | `basetypes.StringType` | Required |  |
| `resource_id` | `basetypes.StringType` | Required |  |
| `source` | `basetypes.StringType` | Optional, Computed | SQL data source (same layer). |
| `sql` | `basetypes.StringType` | Optional, Computed | One read-only SELECT. |
| `unit` | `basetypes.StringType` | Optional, Computed |  |
| `version` | `basetypes.Int64Type` | Computed |  |
| `warn_maximum` | `basetypes.Float64Type` | Optional | Warning above this value. |
| `warn_minimum` | `basetypes.Float64Type` | Optional | Warning below this value. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
