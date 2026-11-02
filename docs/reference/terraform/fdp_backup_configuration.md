# fdp_backup_configuration

Generated from the current provider schema.

Platform metadata recovery to encrypted S3 archives. Declare once. Customer data and external dependency backups remain customer-operated. Configuration requires active Global Admin. Destroy disables scheduling and retains archives and inventory. Import with ID platform_backups.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `bucket` | `basetypes.StringType` | Optional, Computed | Dedicated backup bucket; omit only when source_id supplies it. |
| `enabled` | `basetypes.BoolType` | Optional, Computed | Enable scheduled backups. |
| `endpoint_url` | `basetypes.StringType` | Optional, Computed | HTTPS S3-compatible endpoint. Empty uses AWS S3. |
| `id` | `basetypes.StringType` | Computed |  |
| `interval_hours` | `basetypes.Int64Type` | Optional, Computed | Hours between backup attempts, 1 through 720. |
| `kms_key_id` | `basetypes.StringType` | Optional, Computed | S3 KMS key, only with aws:kms. |
| `object_lock_days` | `basetypes.Int64Type` | Optional, Computed | Optional COMPLIANCE retention. Bucket Object Lock must already be configured. Zero omits lock headers. |
| `prefix` | `basetypes.StringType` | Optional, Computed | Prefix below the integration base prefix. |
| `recovery_public_key` | `basetypes.StringType` | Optional, Computed | RSA public recovery key, minimum 3072 bits. The private key must stay offline. |
| `region` | `basetypes.StringType` | Optional, Computed | AWS signing region. |
| `server_side_encryption` | `basetypes.StringType` | Optional, Computed | Optional extra S3 encryption: AES256 or aws:kms. Client-side encryption is mandatory. |
| `source_id` | `basetypes.StringType` | Optional, Computed | Optional existing external S3 integration. Its STS role is narrowed to the backup prefix. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
