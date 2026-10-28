# fdp_workspace_settings

Generated from the current provider schema.

Workspace limits and size for the whole installation (declare it once). Workspaces never count toward a warehouse's max_concurrent. Destroy restores the installation defaults. Import with ID "workspaces".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `max_running` | `basetypes.Int64Type` | Optional | Workspaces running at once across everyone; more wait in line and start in order. Unset: no total limit. |
| `max_running_per_user` | `basetypes.Int64Type` | Required | Workspaces one person may have running or waiting. |
| `warehouse` | `basetypes.StringType` | Optional | Warehouse whose CPU and memory every editor gets. Unset: the installation default size. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
