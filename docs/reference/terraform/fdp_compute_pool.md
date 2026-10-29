# fdp_compute_pool

Generated from the current provider schema.

A warehouse (compute pool): the size each job, app or Airflow component gets, and how many may run at once. Import by name.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `allowed_groups` | `types.ListType[basetypes.StringType]` | Optional, Computed | Empty: everyone. |
| `auto_suspend_seconds` | `basetypes.Int64Type` | Optional, Computed |  |
| `cpu` | `basetypes.Float64Type` | Required |  |
| `dedicated` | `basetypes.BoolType` | Optional, Computed | Dedicated to allowed_groups (required): not a shared on-demand warehouse, so max_concurrent does not apply and its work never queues or counts elsewhere. Make sure the cluster has nodes for it. |
| `gpu` | `basetypes.Int64Type` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_ids` | `types.ListType[basetypes.StringType]` | Optional, Computed | Layers whose work may use this warehouse (fdp_layer ids). Empty: every layer. |
| `max_concurrent` | `basetypes.Int64Type` | Optional, Computed | Containers running on this warehouse at once: each job, app and Airflow component counts one. More wait in line and start in order. Workspaces do not count. |
| `memory_gb` | `basetypes.Float64Type` | Required |  |
| `name` | `basetypes.StringType` | Required |  |
| `workload_kinds` | `types.ListType[basetypes.StringType]` | Optional, Computed | Kinds of work that may use it: script, notebook, app, dbt, airflow. Empty: every kind. With allowed_groups: "dbt projects of the data engineers in Prod". A declared warehouse outside the scope falls back to the default for the layer and kind (fdp_default_warehouse). |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
