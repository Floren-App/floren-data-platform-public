# fdp_data_product

Generated from the current provider schema.

A data product: tables and views, dashboards, APIs or Kafka topics you own, published together. Everyone in its layer sees its card; groups get access as Consumers of the product (fdp_data_product_consumer) with one of its access options. The first member leads (the card's icon). Domain and classification come from the members' governance (fdp_catalog_governance, fdp_column_classification). Import by product ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `access_resource_id` | `basetypes.StringType` | Computed | The product's own RBAC resource (Consumer and Owner roles). |
| `id` | `basetypes.StringType` | Computed |  |
| `members` | `types.ListType[types.ObjectType["object_path":basetypes.StringType, "resource_id":basetypes.StringType]]` | Required | Objects in the product, lead first. |
| `options` | `provider.JSONType` | Optional, Computed | jsonencode([{ name, description, default, masks = [{ resource_id, table, column, method, expression }], filters = [{ resource_id, table, expression }] }]). method: null, redact, hash, partial or expression. Left out: one Standard option without masks. Only declared keys are compared. |
| `summary` | `basetypes.StringType` | Optional, Computed |  |
| `title` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
