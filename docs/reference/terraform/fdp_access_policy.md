# fdp_access_policy

Generated from the current provider schema.

One cell of the access policy grid: access to data of this classification in this layer needs the form and its review. Without a policy for a cell, no form is needed. Import with "<layer id>/<classification>".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `classification` | `basetypes.StringType` | Required |  |
| `form_id` | `basetypes.StringType` | Required |  |
| `grace_days` | `basetypes.Int64Type` | Optional, Computed | Access that existed before the policy, or whose data was classified higher since, is kept this many days for a form to be approved; then it is removed. |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Required |  |
| `max_days` | `basetypes.Int64Type` | Optional, Computed | Longest approved access. |
| `renew_days` | `basetypes.Int64Type` | Optional, Computed | A renewal, prefilled with the answers, opens this many days before approved access ends. |
| `review_days` | `basetypes.Int64Type` | Optional, Computed | Reviews are due within this many days. |
| `steps` | `types.ListType[types.ObjectType["approvals":basetypes.Int64Type, "role":basetypes.StringType]]` | Required | Review steps in order: data_owner or compliance_reviewer. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
