# fdp_default_warehouse

Generated from the current provider schema.

The warehouse that work without one runs on, and that warehouse choices start with. Without layer_id and workload_kind it is the installation default (declare it once); with either or both it is the default for that layer and/or kind of work. The most specific match wins: layer and kind, the layer, the kind, then the installation default. It cannot be a dedicated warehouse or one that runs Trino workers, a scoped default must be meant for its layer and kind, and a warehouse cannot be deleted while it is a default. Import with ID "default", or "<layer id>/<kind>" using "*" for either part (e.g. "1a0000000001/dbt", "*/dbt").

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Optional | Only for work in this layer (fdp_layer id). |
| `name` | `basetypes.StringType` | Required | The warehouse's name. |
| `workload_kind` | `basetypes.StringType` | Optional | Only for this kind of work: script, notebook, app, dbt or airflow. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
