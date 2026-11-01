# fdp_permission_assignment

Generated from the current provider schema.

An explicit named capability grant on a target, globally or in one layer. Import by assignment ID. Choose exactly one recipient and exactly one role, actions set or custom role. A page gate uses namespace pages and the stable page ID as target_id.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `actions` | `types.SetType[basetypes.StringType]` | Optional | Individually named permissions in this namespace, instead of a built-in role. |
| `app_group_id` | `basetypes.StringType` | Optional |  |
| `custom_role_id` | `basetypes.StringType` | Optional | fdp_custom_role ID, instead of a built-in role or explicit actions. |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Optional | Omit for explicit global scope. Use a layer ID for that layer, or * for explicit all-layer authority (Global Admin only). Global never implicitly includes a layer. |
| `namespace` | `basetypes.StringType` | Required | Capability domain, e.g. pages, network.editor_allowlist, compliance.forms. |
| `principal_id` | `basetypes.StringType` | Optional |  |
| `role` | `basetypes.StringType` | Optional | Viewer, Executor, Contributor, Manager, Admin or Owner. |
| `target_id` | `basetypes.StringType` | Required | Stable target resource or collection ID; page IDs include set-network and set-forms. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
