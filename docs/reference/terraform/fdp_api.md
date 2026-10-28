# fdp_api

Generated from the current provider schema.

An internal API (deployed programs behind JWT/mTLS) or an external API (outbound calls with server-side credentials). Credentials and certificates are secret references, never values. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `direction` | `basetypes.StringType` | Required | internal or external. |
| `endpoint` | `basetypes.StringType` | Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `identity_principal_id` | `basetypes.StringType` | Computed | Internal APIs: the managed identity their programs run as; grant it Executor. |
| `inbound` | `provider.JSONType` | Optional, Computed | Internal APIs: jsonencode({ jwt = {...}, mtls = {...} }). Only declared keys are compared. |
| `layer_id` | `basetypes.StringType` | Optional, Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `outbound` | `provider.JSONType` | Optional, Computed | External APIs: jsonencode({ base_url, headers, auth = {...}, ... }). |
| `protocol` | `basetypes.StringType` | Optional, Computed |  |
| `resource_id` | `basetypes.StringType` | Computed |  |
| `slug` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
