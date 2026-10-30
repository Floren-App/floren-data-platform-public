# fdp_data_policy

Generated from the current provider schema.

A column mask or row filter on one table of a governed SQL schema, enforced by the layer's Trino for everyone who queries the table except the exempt groups and identities. Column masks: method null, redact, hash, partial (last four characters) or expression. Row filters: a condition in expression. Expressions are Trino SQL over the table's own columns and may use is_member('<app group name>'). Requires Owner on the schema. Import by ID.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `column` | `basetypes.StringType` | Optional, Computed | Column masks: the masked column, lowercase. |
| `description` | `basetypes.StringType` | Optional, Computed |  |
| `exempt_groups` | `types.SetType[basetypes.StringType]` | Optional, Computed | App group IDs that see real values / all rows. |
| `exempt_principals` | `types.SetType[basetypes.StringType]` | Optional, Computed | Identity IDs (people or managed identities) exempted. |
| `expression` | `basetypes.StringType` | Optional, Computed | The mask expression (method expression) or the row filter condition. |
| `id` | `basetypes.StringType` | Computed |  |
| `kind` | `basetypes.StringType` | Required | column_mask or row_filter. |
| `method` | `basetypes.StringType` | Optional, Computed | Column masks: null, redact, hash, partial or expression. |
| `normalized_expression` | `basetypes.StringType` | Computed | The expression as the platform stores and enforces it (parsed and formatted, comments removed). |
| `schema_id` | `basetypes.StringType` | Required | The schema (fdp_schema id). |
| `table` | `basetypes.StringType` | Required | Table name, lowercase. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
