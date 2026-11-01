# fdp_kafka_cluster

Generated from the current provider schema.

Manage the built-in layer Kafka registration. Broker compute is controlled by infrastructure.kafka.running on fdp_layer. Destroy removes it from state only; the layer capability, permissions and storage remain. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
