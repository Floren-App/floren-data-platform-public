# fdp_layer

Generated from the current provider schema.

An execution layer (e.g. Dev, Test, Prod). The platform has no built-in layers: they all come from configuration. With `infrastructure`, the layer controller provisions its isolated namespace, Trino and artifact storage; apply waits until it is Ready. A layer that still holds work, data or retained storage is never deleted: destroy is refused until it is emptied and retired. Choose the default layer with fdp_default_layer.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `infrastructure` | `provider.JSONType` | Optional, Computed | Layer infrastructure as JSON (jsonencode), see schemas/layer-infrastructure.schema.json. Only the keys you set are compared; server defaults do not cause drift. Once enabled it cannot be removed. |
| `infrastructure_generation` | `basetypes.Int64Type` | Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `phase` | `basetypes.StringType` | Computed | Provisioning phase of the current generation. |
| `production` | `basetypes.BoolType` | Optional, Computed | A production layer: personal and group secrets are not usable here; work uses service account secrets. |
| `timeouts` | `timeouts.Type` | Optional |  |
| `workspaces` | `basetypes.BoolType` | Optional, Computed | Whether people may create workspaces in this layer. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
