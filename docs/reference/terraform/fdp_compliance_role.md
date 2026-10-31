# fdp_compliance_role

Generated from the current provider schema.

A compliance role an app group holds within a scope: compliance_reviewer (reviews compliance forms), membership_approver (approves people joining groups with justified access) or compliance_admin (forms, policies, classifications, roles). An empty scope list means all. Any change replaces the role. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `app_group_id` | `basetypes.StringType` | Required |  |
| `classifications` | `types.SetType[basetypes.StringType]` | Optional, Computed | Classifications. |
| `group_ids` | `types.SetType[basetypes.StringType]` | Optional, Computed | For membership approvers: app group IDs. |
| `id` | `basetypes.StringType` | Computed |  |
| `layers` | `types.SetType[basetypes.StringType]` | Optional, Computed | Layer IDs. |
| `resource_ids` | `types.SetType[basetypes.StringType]` | Optional, Computed | Objects (and what is under them). |
| `role` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
