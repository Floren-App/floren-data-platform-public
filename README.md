# Floren Data Platform

A desktop hub for analysts. Code, data and compute stay on the server: small editor workspaces
(VS Code or RStudio) for writing code, sized compute pools that start on demand for heavy runs,
and governed data access with secrets in Kubernetes Secrets or OpenBao.

Supports Python, Jupyter notebooks, Streamlit, R, Shiny, dbt and Airflow, in versions admins choose.
100% free/open source.
Workspaces are private; code is shared through git, and programs and apps are **deployed from
git** and kept in sync with their branch (GitOps).

## Documentation

Read the **[searchable documentation site](https://floren-app.github.io/floren-data-platform-public/)**
or start with the [Markdown overview](docs/index.md). Installation, concepts,
workspaces, IAM, SDKs, Terraform, catalog, certificates and the Pokemon walkthrough
live in this repository. API, JSON and provider references are generated from code.

```sh
make docs-install
make docs-serve     # local preview
make docs-build     # strict links, search and publication checks
```

- [docs/query-execution.md](docs/query-execution.md): shared Trino SQL, PostgreSQL/Iceberg catalogs, execution identities and deployment
- [docs/rbac.md](docs/rbac.md): RBAC, SCIM provisioning, schema enforcement, nested folders, and service identities
- [docs/catalog.md](docs/catalog.md): catalog search, governance, freshness/run SLAs, IAM, and native dbt/Airflow/program lineage
- [docs/api-management.md](docs/api-management.md): internal program APIs, outbound REST/SOAP, managed identities, JWT/mTLS and gateway deployment
- [docs/architecture.md](docs/architecture.md): the design
- [docs/platform-storage.md](docs/platform-storage.md): where application metadata, secrets, workspace files and runtime data are stored; workspace startup
- [docs/metadata-sql.md](docs/metadata-sql.md): configurable metadata database, Global Admin SQL catalog, and validated RBAC changes
- [docs/metrics-and-slas.md](docs/metrics-and-slas.md): KPI definitions, dbt quality measurements, scheduled SLAs, history and input gates
- [docs/fdp-json.md](docs/fdp-json.md): defining runnable programs
- [docs/deploying.md](docs/deploying.md): sharing, deploying and GitOps; service accounts and secrets
- [docs/dbt-and-airflow.md](docs/dbt-and-airflow.md): dbt projects (docs with lineage), Airflow pipelines (standalone/distributed), versions
- [docs/data-lake.md](docs/data-lake.md): Iceberg tables and files on RustFS/S3 (Python, R, dbt through Trino)

## Repository layout

| Path | What |
|---|---|
| `apps/desktop` | Tauri 2 + React desktop hub (VS Code-style UI) |
| `services/control-plane` | FastAPI control plane: catalog, grants, pools, workspaces, runs, gateway auth |
| `sdk/python`, `sdk/r` | `fdp` SDKs (`connect()`, `args()`, `run_program()`), the `fdp` CLI, and the RStudio runner |
| `extensions/vscode-fdp` | VS Code runner sidebar with live run consoles |
| `schemas/`, `examples/workspace` | JSON Schema for `fdp.json` ([reference](docs/fdp-json.md)) and a complete example |
| `images/` | Workspace images (code-server, RStudio) and compute images (Python, R) |
| `deploy/compose` | Local stack: Postgres, Keycloak, OpenBao, Traefik, sample warehouse |
| `deploy/helm/fdp` | Kubernetes deployment |

## Run locally

Requirements: Docker, Node 20+, Rust (for the desktop shell), Python 3.9+, and mkcert for local HTTPS
(`brew install mkcert && mkcert -install`, once per developer machine — end users never need it).

```sh
make images      # build workspace + compute images (first time takes a while)
make dev         # start the stack: https://localhost:8090 (gateway), https://localhost:8443 (Keycloak)
                 # (port taken? GATEWAY_PORT=9090 make dev, then use that port in the app)
                 # starts an empty platform; EXAMPLE_STORAGE=1 opts into example storage
make desktop     # run the desktop app (or `make desktop-web` for the browser at https://localhost:1420)
```

For interactive SQL, create a **SQL workspace**: CloudBeaver (open source, the web edition of DBeaver)
with a schema browser, completion, result grids and saved `.sql` scripts, querying through the same
governed engine. See [SQL workspace](docs/sql-workspace.md).

Floren Data Platform does not host data: every database or bucket is an explicit storage integration that
points at storage configured elsewhere. See [storage integrations](docs/storage-integrations.md).

The local stack starts with one application account: **admin/admin**, with platform
administrator permissions. The `admin-init` service provisions it through SCIM, so a fresh
database can be used immediately. Gitea remains available at http://localhost:3000 with
the integration service account `fdp/fdp123`; it does not automatically create
users or example repositories. Register new repositories under Administration → Repositories.
`make seed` also provisions only the local admin by default; finance demo data and users
require the explicit `EXAMPLE_STORAGE=1` opt-in described in the storage integration guide.

Example programs remain in `examples/workspace` as source fixtures. To try them, explicitly
enable/register example storage, create a repository containing those files, and grant the
required schema roles. In a workspace, for example:

```python
import fdp, pandas as pd
pd.read_sql("select region, sum(amount_eur) from sales group by 1", fdp.connect("finance-dw"))
```

Then from the terminal: `fdp run some_script.py --pool large`.

## Tests

```sh
make test        # control plane tests + desktop type-check/build
make test-postgres # backend regressions against a disposable PostgreSQL container
make test-ui     # desktop browser regression tests
```

## Deploy to Kubernetes

Follow the [Kubernetes installation guide](docs/kubernetes-install.md) for the
bundled/external service profiles, image pins, TLS and first sign-in. Keep the Helm
release in your cluster's ArgoCD configuration, and application/workload resources
in separate OpenTofu roots. See [GitOps](docs/gitops.md) and [Terraform](docs/terraform.md).

Requires a CNI that enforces NetworkPolicy and storage classes supporting the
selected volume access modes. Elastic node capacity depends on your cluster setup.

Incoming program APIs use a separate TLS gateway entry point. Configure its public
address and certificates as described in [API management](docs/api-management.md#gateway-deployment).

## License

Floren Data Platform is licensed under the [Apache License 2.0](LICENSE). It uses only free, OSI-approved
open-source components; the container images' third-party software and its licenses are listed in
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). R support (RStudio Server Open Source under AGPL-3.0, R and
Shiny under the GPL) is an optional part of an installation, off unless `r.enabled` is set. Contributions are
welcome under the same license: see [CONTRIBUTING.md](CONTRIBUTING.md); report security problems as described
in [SECURITY.md](SECURITY.md).
