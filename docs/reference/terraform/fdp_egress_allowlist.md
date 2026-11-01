# fdp_egress_allowlist

Generated from the current provider schema.

A global HTTPS host ceiling or one layer's editor host list (Helm egressProxy.enabled). Both must allow a destination. New connections require a signed workspace ticket and verified source address. Private destinations and public package indexes remain blocked. Destroy empties this scope's list. Import as egress (global ceiling) or egress:LAYER_ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `hosts` | `types.MapType[basetypes.StringType]` | Required | Host name (lowercase) to a description: "github.com", or "*.githubusercontent.com" for its subdomains. |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Optional | Layer policy inside the global host ceiling. Omit only for the explicit global ceiling (Owner required). Import as egress:LAYER_ID. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
