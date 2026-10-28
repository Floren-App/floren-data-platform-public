# Third-party software

Floren Data Platform's own code (control plane, desktop app, editor extension, SDKs, Helm chart, OpenTofu
provider) is licensed under the [Apache License 2.0](LICENSE). Every component it uses or ships is under an
OSI-approved open-source license; there are no paid or source-available (e.g. BSL) components.

The container images bundle third-party software under its own licenses, listed below per image. Most is
permissive (Apache-2.0, MIT, BSD, PostgreSQL). **The R images are the exception**: they contain copyleft
software, and they are optional.

## Copyleft components and how to leave them out

| Component | License | In image | What it means |
|---|---|---|---|
| RStudio Server Open Source | AGPL-3.0 | `workspace-rstudio` | Unmodified. The AGPL's network clause concerns *modified* versions offered over a network. Some organizations restrict AGPL software regardless. |
| R | GPL-2.0 or GPL-3.0 | `workspace-rstudio`, `compute-r` | Unmodified; R's source is available from CRAN. |
| Shiny, RPostgres | GPL-3.0 | `workspace-rstudio`, `compute-r` | Unmodified R packages from CRAN. |
| DBI | LGPL-2.1 or later | `workspace-rstudio`, `compute-r` | Unmodified R package from CRAN. |
| data.table | MPL-2.0 | `compute-r` | Weak (file-level) copyleft; unmodified. |
| OpenBao (when used as the secret store) | MPL-2.0 | not built here; your own deployment | Weak (file-level) copyleft. |

**R is an optional part of an installation.** Without it (`r.enabled: false` in the Helm values), the
`workspace-rstudio` and `compute-r` images are not used or pulled, and no GPL or AGPL software runs. Python, SQL,
dbt and Airflow do not depend on R.

Running these images does not change the license of your own programs, data or the platform: they run as
separate programs, and nothing links them into Floren Data Platform's code.

## Per image

| Image | Main components (license) |
|---|---|
| `control-plane` (also the egress proxy and layer controller) | Python (PSF), FastAPI, Starlette, Pydantic (MIT), SQLAlchemy (MIT), uvicorn (BSD-3), httpx (BSD-3), PyJWT (MIT), cryptography (Apache-2.0/BSD), psycopg (LGPL-3.0, unmodified, dynamically used), kubernetes client (Apache-2.0), hvac (Apache-2.0), boto3 (Apache-2.0), sqlglot (MIT), packaging (Apache-2.0/BSD), python-multipart (Apache-2.0), Git (GPL-2.0, used as a separate program), Debian (various free licenses) |
| `workspace-vscode` | code-server and VS Code OSS (MIT); from Open VSX: Python and Jupyter extensions (MIT), Ruff (MIT), basedpyright (MIT); Python (PSF), Jupyter, ipykernel, pandas (BSD-3), Streamlit (Apache-2.0), Ruff (MIT); Floren Data Platform extension and SDK (Apache-2.0) |
| `workspace-sql` | CloudBeaver Community (Apache-2.0); Python (PSF) |
| `web` (the app in the browser) | nginx (BSD-2-Clause); the app's own code (Apache-2.0) with React, React DOM (MIT), oidc-client-ts (Apache-2.0), and their dependencies (MIT, ISC, BSD) |
| `workspace-rstudio` *(optional, R)* | RStudio Server Open Source (AGPL-3.0); R (GPL-2/3); rocker images (GPL-2.0); Shiny, RPostgres (GPL-3.0); DBI (LGPL-2.1+); httr2, dplyr, ggplot2, jsonlite, rstudioapi, rappdirs (MIT); Floren Data Platform R SDK (Apache-2.0) |
| `compute-python` | Python (PSF); Floren Data Platform SDK (Apache-2.0) with its optional extras |
| `compute-r` *(optional, R)* | R (GPL-2/3); rocker images (GPL-2.0); Shiny, RPostgres (GPL-3.0); DBI (LGPL-2.1+); bslib, httr2, dplyr, ggplot2, jsonlite, rappdirs (MIT); data.table (MPL-2.0); Floren Data Platform R SDK (Apache-2.0) |
| `compute-dbt` | dbt Core and dbt-trino (Apache-2.0); Python (PSF) |
| `airflow` | Apache Airflow (Apache-2.0); Python (PSF) |

Images the Helm chart pulls as they are: Trino (Apache-2.0), Keycloak (Apache-2.0), PostgreSQL (PostgreSQL
License), Traefik (MIT), Valkey (BSD-3), RustFS (Apache-2.0).

People may install other packages in their workspaces and programs, from the platform's package repository; those
are under their own licenses, which the person or organization installing them is responsible for.
