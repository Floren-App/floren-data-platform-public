# fdp_column_classification

Generated from the current provider schema.

Classification and tags of a table's columns. The table's level follows the highest of its columns (and is never below them); masked columns count as their level's masked_as for access. Only the declared columns are managed; destroy clears them. Import with "<schema resource id>/<table>".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `columns` | `types.MapType[types.ObjectType["classification":basetypes.StringType, "tags":types.SetType[basetypes.StringType]]]` | Required | Per column name (lower case). |
| `id` | `basetypes.StringType` | Computed | Catalog asset ID of the table. |
| `object_path` | `basetypes.StringType` | Required | The table or view. |
| `resource_id` | `basetypes.StringType` | Required | The schema resource. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
