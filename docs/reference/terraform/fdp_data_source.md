# fdp_data_source

Generated from the current provider schema.

A storage integration (PostgreSQL, Iceberg tables on S3, or S3 files) in one layer. Import by name.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `config` | `provider.JSONType` | Optional, Computed | Location settings as JSON (jsonencode), e.g. host/port/database/catalog. Only the keys you set are compared. |
| `credentials_wo_version` | `basetypes.Int64Type` | Optional | Change to send the write-only credentials again. |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `egress` | `types.ListType[basetypes.StringType]` | Optional, Computed | Network ranges workloads may reach for this source. |
| `id` | `basetypes.StringType` | Computed |  |
| `kind` | `basetypes.StringType` | Required | postgres, iceberg or s3. |
| `layer_id` | `basetypes.StringType` | Optional, Computed | Defaults to Non-Prod. Cannot change. |
| `name` | `basetypes.StringType` | Required |  |
| `provisioner_secret_wo` | `types.MapType[basetypes.StringType]` | Optional, Sensitive, Write-only | Access-setup credential. Write-only: never stored in state or returned by the API. Sent on create and whenever credentials_wo_version changes. |
| `resource_id` | `basetypes.StringType` | Computed | The data source as an RBAC object, for fdp_role_assignment on the whole source. |
| `secret_wo` | `types.MapType[basetypes.StringType]` | Optional, Sensitive, Write-only | Engine/native credential. Write-only: never stored in state or returned by the API. Sent on create and whenever credentials_wo_version changes. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
