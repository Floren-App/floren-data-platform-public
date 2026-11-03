# fdp_backup_destination

Generated from the current provider schema.

Reusable S3 backup destination. Recovery keys and credentials stay in the installation-selected vault; recovery_key_ref names an existing secret. Registration requires active Global Admin. Destroy retains archives and fails while any plan still refers to it.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `bucket` | `basetypes.StringType` | Optional, Computed |  |
| `endpoint_url` | `basetypes.StringType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `kms_key_id` | `basetypes.StringType` | Optional, Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `object_lock_days` | `basetypes.Int64Type` | Optional, Computed |  |
| `prefix` | `basetypes.StringType` | Optional, Computed |  |
| `recovery_key_ref` | `basetypes.StringType` | Required |  |
| `region` | `basetypes.StringType` | Optional, Computed |  |
| `server_side_encryption` | `basetypes.StringType` | Optional, Computed |  |
| `source_id` | `basetypes.StringType` | Optional, Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
