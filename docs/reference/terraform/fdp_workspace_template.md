# fdp_workspace_template

Generated from the current provider schema.

A group's default workspace. Every member of the app group gets their own private workspace from it, created stopped, including people who join later; leaving the group removes theirs (files stay in its volume). Destroy removes the template's workspaces. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `editor` | `basetypes.StringType` | Required | vscode, rstudio or sql. |
| `group_id` | `basetypes.StringType` | Required | The app group whose members get it. |
| `id` | `basetypes.StringType` | Computed |  |
| `layer_id` | `basetypes.StringType` | Required | A layer that allows workspaces. |
| `name` | `basetypes.StringType` | Required | The workspace name each member sees. |
| `repository_ids` | `types.ListType[basetypes.StringType]` | Optional, Computed | Repositories cloned at first start. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
