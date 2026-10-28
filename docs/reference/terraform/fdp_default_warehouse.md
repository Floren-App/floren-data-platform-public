# fdp_default_warehouse

Generated from the current provider schema.

The warehouse that work without one runs on, and that warehouse choices start with (declare it once). It cannot be a dedicated warehouse or one that runs Trino workers, and it cannot be deleted while it is the default. Import with ID "default".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `name` | `basetypes.StringType` | Required | The warehouse's name. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
