# fdp_runtime

Generated from the current provider schema.

An additional runtime version people can choose in fdp.json (e.g. Python 3.11 next to the installation's 3.12), with its image. The versions the installation ships come from the Helm values (runtime.images, runtime.versions) and follow them on every upgrade; do not declare those here. Import as kind/version.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `default` | `basetypes.BoolType` | Optional, Computed | Make it the version used when fdp.json names none. |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `enabled` | `basetypes.BoolType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `image` | `basetypes.StringType` | Required | The compute image providing this version. |
| `kind` | `basetypes.StringType` | Required | python, r, dbt or airflow. |
| `version` | `basetypes.StringType` | Required | e.g. 3.11. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
