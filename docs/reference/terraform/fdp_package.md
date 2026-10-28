# fdp_package

Generated from the current provider schema.

A public package in the platform's package repository: apply imports it from PyPI or CRAN with its missing dependencies (Python: built packages only) and waits until the import is done. If the version disappears from the repository, the next apply imports it again. A new version imports that one; earlier versions stay. Destroy only stops declaring it: imported files are kept, since programs may depend on them. Import as kind/name/version.

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `id` | `basetypes.StringType` | Computed |  |
| `kind` | `basetypes.StringType` | Required | python (from PyPI) or r (from CRAN). |
| `name` | `basetypes.StringType` | Required | The package name, e.g. pandas or dplyr. |
| `origin` | `basetypes.StringType` | Computed | pypi or cran. |
| `timeouts` | `timeouts.Type` | Optional |  |
| `version` | `basetypes.StringType` | Optional, Computed | Exact version, e.g. 2.2.3. Unset: the newest at the first apply, then kept (set it to upgrade). |

Nested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.
