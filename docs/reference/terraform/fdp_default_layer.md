# fdp_default_layer

Generated from the current provider schema.

The layer that work without a layer goes to (declare it once). Without it, an installation with one layer uses that layer and one with several asks for a layer. The default layer cannot be deleted. Import with ID "default".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
