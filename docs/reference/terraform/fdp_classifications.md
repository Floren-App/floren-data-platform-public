# fdp_classifications

Generated from the current provider schema.

The installation's classifications, from least to most sensitive; declare once. Columns and objects are classified with these names; access policies refer to them. masked_as says what masked data of a level counts as (itself or a lower level). A level still in use cannot be removed. Destroy restores public, internal, confidential and restricted. Import with ID "classifications".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `levels` | `types.ListType[types.ObjectType["description":basetypes.StringType, "masked_as":basetypes.StringType, "name":basetypes.StringType]]` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
