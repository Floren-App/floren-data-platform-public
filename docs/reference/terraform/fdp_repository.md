# fdp_repository

Generated from the current provider schema.

A Git repository programs are deployed from. Only the control plane talks to Git. Import by name.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `allowed_groups` | `types.ListType[basetypes.StringType]` | Optional, Computed | Who may deploy from it; empty = everyone. |
| `credentials_wo` | `types.MapType[basetypes.StringType]` | Optional, Sensitive, Write-only | {username, token} with read access. Write-only: never stored in state; sent on create and when credentials_wo_version changes. |
| `credentials_wo_version` | `basetypes.Int64Type` | Optional |  |
| `default_branch` | `basetypes.StringType` | Optional, Computed |  |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `has_credentials` | `basetypes.BoolType` | Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `url` | `basetypes.StringType` | Required | https:// clone URL (http:// for an internal Git server). |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
