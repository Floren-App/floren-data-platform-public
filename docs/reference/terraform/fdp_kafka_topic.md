# fdp_kafka_topic

Generated from the current provider schema.

A governed managed Kafka topic. Create/update waits up to 15 minutes for Strimzi readiness; destroy deletes its messages. Import by ID. Grants use fdp_role_assignment.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `cleanup_policy` | `basetypes.StringType` | Optional, Computed |  |
| `cluster_id` | `basetypes.StringType` | Required |  |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `partitions` | `basetypes.Int64Type` | Optional, Computed |  |
| `phase` | `basetypes.StringType` | Computed |  |
| `replication_factor` | `basetypes.Int64Type` | Optional, Computed | At least 3 for multi-broker clusters. Replica reassignment is a separate controlled operation. |
| `retention_ms` | `basetypes.Int64Type` | Optional, Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
