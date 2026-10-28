# fdp_role_assignment

Generated from the current provider schema.

A built-in role on one object (schema, source, folder, workload, API...) for an app group or directly for a user/service identity. Import as <resource_id>/group/<id> or <resource_id>/principal/<id>.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `app_group_id` | `basetypes.StringType` | Optional |  |
| `id` | `basetypes.StringType` | Computed |  |
| `principal_id` | `basetypes.StringType` | Optional |  |
| `resource_id` | `basetypes.StringType` | Required |  |
| `role` | `basetypes.StringType` | Required | e.g. Reader, Contributor, Admin, Owner, Executor, Consumer; on the package repository (resource_id "packages"): Publisher, Maintainer or Owner. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
