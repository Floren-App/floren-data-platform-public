# fdp_egress_allowlist

Generated from the current provider schema.

Host names VS Code and RStudio workspaces may reach over HTTPS, through the platform's egress proxy (declare it once; Helm egressProxy.enabled). Everything else on the internet stays unreachable, and names resolving to private addresses are refused. Changes apply to running workspaces within seconds. Destroy empties the list. Import with ID "egress".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `hosts` | `types.MapType[basetypes.StringType]` | Required | Host name (lowercase) to a description: "github.com", or "*.githubusercontent.com" for its subdomains. |
| `id` | `basetypes.StringType` | Computed |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
