# fdp_data_product_consumer

Generated from the current provider schema.

An app group's access to a data product: Consumer on the product with one of its access options (its masks and row filters). The group may then use everything in the product. Destroy takes the access away. Import with "<product id>/<app group id>".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `app_group_id` | `basetypes.StringType` | Required |  |
| `id` | `basetypes.StringType` | Computed |  |
| `option` | `basetypes.StringType` | Optional, Computed | Name of the access option; left out: the product's default option. |
| `product_id` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
