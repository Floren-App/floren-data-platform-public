# fdp_folder

Generated from the current provider schema.

A folder that organizes workloads and their permissions (inherited by everything inside). Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `branch` | `basetypes.StringType` | Optional | Promotion rule: only workloads from this branch may go into this folder (an environment folder). |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Optional, Computed | Bind everything inside to one layer. |
| `name` | `basetypes.StringType` | Required |  |
| `parent_id` | `basetypes.StringType` | Optional, Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
