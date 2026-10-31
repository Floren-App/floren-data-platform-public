# fdp_app_profile

Generated from the current provider schema.

An app profile: the features (pages, navigation and the API actions behind them) a group gets. Give it to groups with fdp_app_group.app_profile. People get the profile of their active group; Global Admin has everything; object permissions still decide what can be opened. Features: products.browse, products.request, products.publish, catalog.objects, workspaces, workloads.view, workloads.manage, storage, packages, kafka, apis, usage, security. A feature brings what it needs (products.request brings products.browse). Import by name.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `default` | `basetypes.BoolType` | Optional, Computed | The profile of groups without one. Exactly one profile is the default; making this one the default takes it from the other. |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `features` | `types.SetType[basetypes.StringType]` | Required | Feature keys; the server adds what they need, so declare those too to avoid drift. |
| `id` | `basetypes.StringType` | Computed |  |
| `name` | `basetypes.StringType` | Required |  |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
