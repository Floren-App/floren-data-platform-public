# fdp_custom_role

Generated from the current provider schema.

A named set of registered action permissions. Global Admin must be active to edit a definition because edits affect all recipients. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `actions` | `types.SetType[basetypes.StringType]` | Required |  |
| `id` | `basetypes.StringType` | Computed |  |
| `name` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
