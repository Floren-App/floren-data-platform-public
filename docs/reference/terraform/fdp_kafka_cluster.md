# fdp_kafka_cluster

Generated from the current provider schema.

Register a layer's managed Kafka for topic IAM. Enable infrastructure.kafka on fdp_layer first. Destroy unregisters metadata only; brokers and storage are retained. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
