# fdp_workload

Generated from the current provider schema.

A program deployed from a repository branch (job, app, dbt, Airflow or continuous stream), kept in sync with the branch. Apply waits until it is live. Promotion, branch protection and execution-identity rules still apply. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `auto_update` | `basetypes.BoolType` | Optional, Computed |  |
| `branch` | `basetypes.StringType` | Required |  |
| `deployed_sha` | `basetypes.StringType` | Computed |  |
| `env` | `types.MapType[basetypes.StringType]` | Optional, Computed |  |
| `execution_mode` | `basetypes.StringType` | Optional, Computed |  |
| `folder_id` | `basetypes.StringType` | Optional, Computed | Folder whose permissions it inherits. |
| `id` | `basetypes.StringType` | Computed |  |
| `kind` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Optional, Computed |  |
| `managed_identity_grants` | `types.MapType[basetypes.StringType]` | Optional, Computed | Resource ID -> role for this workload's built-in managed identity. Grants are installed before waiting for deployment readiness and reconciled on refresh. Do not also manage the same assignments with fdp_role_assignment. |
| `program` | `basetypes.StringType` | Required | Program name in fdp.json. |
| `repository_id` | `basetypes.StringType` | Required |  |
| `resource_id` | `basetypes.StringType` | Computed |  |
| `run_as` | `basetypes.StringType` | Optional | Optional service account override. With execution_mode = fixed and no override, runs as this workload's built-in managed identity. |
| `secrets` | `types.MapType[basetypes.StringType]` | Optional, Computed | Environment variable -> secret reference (never values). |
| `service_principal_id` | `basetypes.StringType` | Computed |  |
| `slug` | `basetypes.StringType` | Required | Address name: /p/<slug>/. |
| `status` | `basetypes.StringType` | Computed |  |
| `timeouts` | `timeouts.Type` | Optional |  |
| `title` | `basetypes.StringType` | Optional, Computed |  |
| `url` | `basetypes.StringType` | Computed |  |
| `warehouse` | `basetypes.StringType` | Optional, Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
