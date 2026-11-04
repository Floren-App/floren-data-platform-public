# fdp_role_assignment

Generated from the current provider schema.

A built-in role on one object (schema, source, folder, workload, API...) for an app group or directly for a user/service identity. A recipient can hold several roles on one object, one resource each. Import as <resource_id>/group/<id>/<role> or <resource_id>/principal/<id>/<role>.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `app_group_id` | `basetypes.StringType` | Optional |  |
| `id` | `basetypes.StringType` | Computed |  |
| `justification_id` | `basetypes.StringType` | Optional | An approved compliance case (its ID, shown in the app) for this access, needed when an access policy applies to the object's data in its layer. |
| `principal_id` | `basetypes.StringType` | Optional |  |
| `resource_id` | `basetypes.StringType` | Required |  |
| `role` | `basetypes.StringType` | Required | Reader (see the object and its configuration), Contributor (change its settings), DataReader (read its data), DataContributor (change its data), Executor (trigger on-demand workloads), Viewer (find a data product and ask for access), AccessAdministrator, Admin or Owner, where the object offers it. Changing it replaces the role in place. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
