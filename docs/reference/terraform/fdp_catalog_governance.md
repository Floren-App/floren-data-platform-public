# fdp_catalog_governance

Generated from the current provider schema.

Catalog documentation and stewardship of one governed object (schema, workload, API...). Certification requires description, domain, owner group, steward and classification. Destroy resets it. Import by resource ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `classification` | `basetypes.StringType` | Optional, Computed | unclassified or a configured classification (fdp_classifications). A table is never below its columns. |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `domain` | `basetypes.StringType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed | Catalog asset ID. |
| `object_path` | `basetypes.StringType` | Optional | A table or view in the schema named by resource_id; left out: the resource itself. |
| `owner_group_id` | `basetypes.StringType` | Optional | Accountable app group. |
| `resource_id` | `basetypes.StringType` | Required |  |
| `status` | `basetypes.StringType` | Optional, Computed | draft, reviewed, certified or deprecated. |
| `steward` | `basetypes.StringType` | Optional, Computed |  |
| `tags` | `types.SetType[basetypes.StringType]` | Optional, Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
