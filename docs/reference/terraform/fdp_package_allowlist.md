# fdp_package_allowlist

Generated from the current provider schema.

Which public packages may be imported from PyPI and CRAN (declare once). While enabled, imports from the app, the API and fdp_package may only request a listed package; its dependencies come along. Packages published in the platform are not affected. Destroy turns it off. Import with ID "package_allowlist".

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `enabled` | `basetypes.BoolType` | Optional, Computed |  |
| `id` | `basetypes.StringType` | Computed |  |
| `python` | `types.MapType[basetypes.StringType]` | Optional, Computed | Package name (canonical: lowercase, "-" between words) to a description; "scikit-*" for a family. |
| `r` | `types.MapType[basetypes.StringType]` | Optional, Computed | CRAN package name (as written on CRAN, e.g. DBI) to a description; "tidy*" for a family. |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
