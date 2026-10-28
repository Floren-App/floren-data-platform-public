# fdp_secret

Generated from the current provider schema.

A shared secret (group or service-account scope) that programs reference by name, or a certificate (public certificate in state, private key write-only). Import as <scope>/<owner>/<name>.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `ca_chain` | `basetypes.StringType` | Optional | PEM issuing chain (public). |
| `certificate` | `basetypes.StringType` | Optional | PEM certificate (public). |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `kind` | `basetypes.StringType` | Optional, Computed | string or certificate. |
| `name` | `basetypes.StringType` | Required |  |
| `owner` | `basetypes.StringType` | Required | Group name or service account name. |
| `private_key_wo` | `basetypes.StringType` | Optional, Sensitive, Write-only | PEM private key. Write-only: never stored in state or returned; sent on create and when value_wo_version changes. |
| `reference` | `basetypes.StringType` | Computed | How programs reference it (fdp.json secrets). |
| `scope` | `basetypes.StringType` | Required | group or sa. |
| `value_wo` | `basetypes.StringType` | Optional, Sensitive, Write-only | The secret value. Write-only: never stored in state or returned; sent on create and when value_wo_version changes. |
| `value_wo_version` | `basetypes.Int64Type` | Optional |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
