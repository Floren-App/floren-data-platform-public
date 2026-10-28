# fdp_app_group

Generated from the current provider schema.

A Floren Data Platform app group: the unit that receives roles. Members are directory identities (managed by SCIM/your IdP) bound with fdp_app_group_member. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `owners` | `types.SetType[basetypes.StringType]` | Required | Principal IDs who manage the group's members. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
