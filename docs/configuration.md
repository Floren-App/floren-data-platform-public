# Where configuration lives: Helm or OpenTofu

Floren Data Platform has two layers of configuration. Each setting lives in **one** of them.

| | **Helm values** (`deploy/helm/fdp/values.yaml`) | **OpenTofu** (`registry.opentofu.org/floren-app/fdp`) or the app |
|---|---|---|
| What | **How the platform is installed**: images, cluster resources, storage, networks, identity provider connection, secrets, ports | **What the platform contains and its policies**: layers, warehouses, limits, groups, roles, data sources, workloads, allowlists |
| Stored in | Kubernetes objects and the control plane's environment | The platform's metadata database |
| Applied | `helm upgrade` (restarts pods) | Live, within seconds; no restarts |
| Who | The cluster operator | Platform administrators (OpenTofu, or the desktop app's Administration pages) |

**Guarantees**

- A Helm upgrade **never changes** anything OpenTofu or the app configured. The only thing the control plane
  writes from Helm values at start is its own [installation runtimes](#runtimes), whose images OpenTofu and the
  app cannot change.
- Where a Helm value and an OpenTofu resource cover the same thing, the Helm value is only a **default**:
  once OpenTofu (or the app) sets it, that wins, also after later Helm upgrades. Removing the OpenTofu resource
  restores the Helm default.
- The app and OpenTofu edit the same settings. A change made in the app shows up as drift in `tofu plan`, and
  the next `tofu apply` restores the declared value. Pick one per setting: in practice, declare in OpenTofu what
  you want reviewed and versioned, and leave the rest to the app.

## Settings covered by both

| Setting | Helm value (default) | OpenTofu / app (wins once set) |
|---|---|---|
| Workspace limit per person | `runtime.maxRunningPerUser` | `fdp_workspace_settings.max_running_per_user` |
| Editor size | `runtime.workspaceCpu`, `runtime.workspaceMemoryGb` | `fdp_workspace_settings.warehouse` |
| Total running workspaces | — (no limit) | `fdp_workspace_settings.max_running` |

## Runtimes

Runtimes are the Python, R, dbt and Airflow versions programs choose in `fdp.json`. They come from both places
and never overlap:

- **Installation runtimes (Helm)**: one version per language, `runtime.images.compute*` with `runtime.versions`.
  Every start applies the Helm images to them, so upgrades reach runs. They are marked *Installation* in the app,
  where they can be disabled or made (not) default, but their image is not edited. When Helm moves to a new
  version, that one becomes the default (if the old one was) and the old one stays as an ordinary version.
- **Additional versions (OpenTofu or app)**: `fdp_runtime` (for example Python 3.11 next to 3.12). Helm never
  touches them. Do not declare the installation's versions as `fdp_runtime`.

## Settings that work together (no overlap)

| Helm value | OpenTofu / app |
|---|---|
| `egressProxy.enabled`: runs the workspace egress proxy; `FDP_EGRESS_PROXY_TOKEN` authenticates it | `fdp_egress_allowlist`: global host ceiling plus explicit lists per layer |
| `packages.*`: package storage, upstream PyPI/CRAN, import platforms | `fdp_role_assignment` on `packages`: who publishes and imports |
| `layerController.*`, including the cluster's `network` CIDRs | `fdp_layer` with `infrastructure`: each layer's Trino, storage and quotas |
| `registryCredentials`: pull secret for private images | `fdp_runtime.image`: may point at that registry |
| `oidc.*`, `keycloak.*`: identity provider connection and directory sync | `fdp_app_group`, `fdp_app_group_member`: groups and their members |
| `apiGateway.*`: the program APIs' public address and entry point | `fdp_api`, `fdp_api_operation`: the APIs |
| `secretManagement.*`, `openbao.*`: the secret store backend | `fdp_secret`, credentials of `fdp_data_source` and `fdp_repository` |

## Helm only

Optional parts (`web.enabled`: the app in the browser at `publicUrl`; `r.enabled`: RStudio, R and Shiny programs,
R packages; `egressProxy.enabled`), images (`image`, `web.image`,
`runtime.images.vscode/rstudio/sql`, Airflow helpers), `replicas`, `resources`, `publicUrl`,
TLS (`tls`, `trust`, `certificateMonitoring`), `runtime.namespace`, `runtime.storageClass`,
`runtime.idleTimeoutSeconds`, `dataVolume`, `metadataDatabase`, `metadataSql` (the platform's own SQL catalog),
`gitopsIntervalSeconds`, `bootstrap`, `existingSecret`, `traefik`, `metrics` (ServiceMonitor).

## OpenTofu (or app) only

Public packages and the package allowlist (`fdp_package`, `fdp_package_allowlist`), query log retention and SQL text (`fdp_query_log_settings`), layers and the default layer (`fdp_layer`, `fdp_default_layer`), warehouses and the default warehouse
(`fdp_compute_pool`, `fdp_default_warehouse`), workspace templates, data sources and schemas, app groups and
roles, folders, service accounts, secrets, Git repositories, workloads, APIs, catalog governance, SLAs and
metrics. See the provider's README for every resource.

People's own settings (Git credentials, which credential a workspace uses) live in the app only.

## For contributors

A new setting goes in exactly one place: Helm when it is about installing (it needs a restart, a Kubernetes
object or a secret), OpenTofu otherwise. If a Helm value is a default for an OpenTofu setting, the stored
setting must always win, the Helm value must never be written over it at start, and the setting must be listed
in the table above.

## Identity provider clients

With the bundled Keycloak, the app's sign-in client follows the Helm values: after every install and upgrade a
short job (`fdp-keycloak-clients`) sets its redirect addresses (`keycloak.desktopRedirectUris`, plus `publicUrl`
when `web.enabled`). Everything else in the realm (users, groups, roles) is managed in Keycloak or by the
directory, never by Helm. With another identity provider, register `publicUrl/*` as a redirect address of the
app's public client yourself.
