# fdp_sla_policy

Generated from the current provider schema.

Service level of one object: freshness for datasets; success rate, deadlines, input gates and enforcement for deployed workloads. Destroy removes the policy. Import by resource ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed | Catalog asset ID. |
| `policy` | `provider.JSONType` | Optional, Computed | jsonencode({ freshness_minutes, success_rate_percent, window_hours, deadline_time, timezone, weekdays, required_assets, enforcement, ... }). Only declared keys are compared. |
| `resource_id` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
