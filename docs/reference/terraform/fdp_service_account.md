# fdp_service_account

Generated from the current provider schema.

A layer-bound service identity that deployed programs can run as (run_as). Grant it roles with fdp_role_assignment (principal_id from the directory). Destroy disables it. Import by name. Tokens stay runtime state.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `disabled` | `basetypes.BoolType` | Optional, Computed |  |
| `groups` | `types.ListType[basetypes.StringType]` | Optional, Computed | Legacy data access groups. |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Optional, Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `principal` | `basetypes.StringType` | Computed | sa:<name>, as used by run_as. |
| `resource_id` | `basetypes.StringType` | Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
