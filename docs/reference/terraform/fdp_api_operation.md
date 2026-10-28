# fdp_api_operation

Generated from the current provider schema.

One operation of an API. Internal: the deployed program it runs (same layer) and how request fields map to arguments. External: extra headers and timeout. Import as <api_id>/<operation_id>.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `api_id` | `basetypes.StringType` | Required |  |
| `config` | `provider.JSONType` | Optional, Computed | Internal: jsonencode({ workload_id, mode, args, ... }); external: jsonencode({ headers, timeout_seconds }). Only declared keys are compared. |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `group` | `basetypes.StringType` | Optional, Computed | Permission group within the API. |
| `id` | `basetypes.StringType` | Computed |  |
| `method` | `basetypes.StringType` | Optional, Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `path` | `basetypes.StringType` | Optional, Computed |  |
| `resource_id` | `basetypes.StringType` | Computed | Grant Consumer/Caller roles on this. |
| `soap_action` | `basetypes.StringType` | Optional, Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
