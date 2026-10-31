# fdp_access_form

Generated from the current provider schema.

A compliance form people fill in to justify access under an access policy. Applying a changed definition publishes it as a new version; cases keep the version they were filled in with. Field types: short_text, long_text, choice, multi_choice, date, number, statement, person, file. Import by form ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `definition` | `provider.JSONType` | Required | jsonencode({ pages = [{ id, title, intro, show_if = { field, equals \| contains }, fields = [{ id, label, help, type, required, options, show_if }] }] }). Only declared keys are compared. |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `name` | `basetypes.StringType` | Required |  |
| `version` | `basetypes.Int64Type` | Computed | The published version. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
