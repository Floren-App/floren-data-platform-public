# Floren Data Platform provider for OpenTofu

Configure Floren Data Platform declaratively: layers (including isolated infrastructure), storage
integrations, schemas, app groups and role assignments, compute pools, Git repositories and
workloads. Every change goes through the same validated API as the desktop app.

Terraform itself has been under the Business Source License since 1.6, so this provider is built
and tested with [OpenTofu](https://opentofu.org) (MPL-2.0). It uses HashiCorp's MPL-licensed
plugin framework and works with any tool that speaks the same plugin protocol.

## Authentication

Use an **automation identity** with the platform-admin role, never a person's account.

With the bundled Keycloak, enable the client in the Helm values:

```yaml
keycloak:
  automationClient:
    enabled: true   # client "fdp-automation"; secret key automation-client-secret
```

The directory sync provisions its service account so it is a recognised administrator. The
client is created only for a new realm; on an existing realm, add a confidential client with a
service account, the `fdp-admin` realm role and an audience mapper for `fdp-api`.
With another identity provider, create the equivalent application with client credentials and
the admin role claim.

```hcl
provider "fdp" {
  endpoint      = "https://fdp.example.com/api"
  token_url     = "https://idp.example.com/realms/fdp/protocol/openid-connect/token"
  client_id     = "fdp-automation"
  # client_secret from FDP_CLIENT_SECRET
  owner_group_id = "<app group that owns created objects>"
}
```

Every setting can also come from the environment: `FDP_ENDPOINT`, `FDP_TOKEN`,
`FDP_TOKEN_URL`, `FDP_CLIENT_ID`, `FDP_CLIENT_SECRET`, `FDP_CA_FILE` and
`FDP_OWNER_GROUP_ID`. The provider renews client-credential tokens itself.
`owner_group_id` makes ownership explicit: automation never depends on someone's sidebar
selection.

## Resources

These resources configure what the platform contains; how it is installed (images, storage, networks, identity
provider) is Helm's. A few Helm values are defaults that a resource here overrides (`fdp_workspace_settings`), and
the installation's own runtimes come from Helm (`fdp_runtime` adds more). A Helm upgrade never changes what is
declared here. See `docs/configuration.md` for what lives where.

| Resource | Notes | Import ID |
|---|---|---|
| `fdp_layer` | There are no built-in layers; define each one. `production = true` keeps personal and group secrets out. `infrastructure = jsonencode({...})` turns on the isolated layer; apply waits until it is Ready (`timeouts`). Destroying a layer with infrastructure retires it (compute to zero) and keeps it with its storage; an unused layer without infrastructure is deleted. Installations upgraded from built-in layers import Prod (`1a0000000001`) and Non-Prod (`1a0000000002`) once. | layer ID |
| `fdp_default_layer` | Declare once: where work that names no layer goes. Without it, one layer is the default and several layers require a choice. The default layer cannot be deleted. | `default` |
| `fdp_compute_pool` | A warehouse: size and `max_concurrent` (jobs, apps and Airflow components running at once; more wait in line). A warehouse with run history, the default one, the one workspaces use, or one a layer's Trino workers use cannot be deleted. A warehouse that runs Trino workers is Trino's alone: jobs, apps, Airflow and workspaces cannot use it. An initial layer is kept on destroy with its Trino, so keep its worker warehouse outside the configuration you destroy. | name |
| `fdp_compute_pool` with `dedicated = true` | A team's own warehouse (e.g. for business-critical Airflow): requires `allowed_groups`, has no slot limit and never queues; only those groups deploy or run on it. Not for Trino workers or workspaces. | name |
| `fdp_default_warehouse` | Declare once: the warehouse work without one runs on, and that warehouse choices start with. If a program's `fdp.json` names a warehouse that does not exist, its runs use this one. Not a dedicated or Trino warehouse. | `default` |
| `fdp_workspace_template` | A group's default workspace: every member of the app group gets their own private workspace from it, created stopped (also later members); leaving removes theirs. `repository_ids` are cloned at first start. | ID |
| `fdp_egress_allowlist` | Host names VS Code and RStudio workspaces may reach over HTTPS through the egress proxy (Helm `egressProxy.enabled`), as `hosts = { "github.com" = "Git" }`; `*.example.com` covers subdomains. Declare once; destroy empties it. | `egress` |
| `fdp_runtime` | An additional runtime version (e.g. Python 3.11) and its image. The versions the installation ships come from Helm (`runtime.images`, `runtime.versions`) and follow it on upgrades; they are not declared here. | `kind/version` |
| `fdp_query_log_settings` | Declare once: how long the query log keeps every Trino query (`retention_days`, default 90) and whether it keeps SQL text (`record_sql`, default false: queries can contain literal values). | `query_log` |
| `fdp_package` | A public package in the package repository: apply imports it from PyPI (`kind = "python"`) or CRAN (`"r"`) with its missing dependencies and waits for the import. `version` unset: the newest, then kept. A deleted or yanked version is imported again at the next apply; destroy keeps the files. | `kind/name/version` |
| `fdp_package_allowlist` | Declare once: which public packages may be imported at all (`python`, `r`: name to description; `scikit-*` for a family). Applies to the app, the API and `fdp_package`; dependencies come along; published packages are not affected. Destroy turns it off. | `package_allowlist` |
| `fdp_workspace_settings` | Declare once: the warehouse that sizes every editor, the total (`max_running`) and per-person (`max_running_per_user`) workspace limits. Destroy restores the defaults. | `workspaces` |
| `fdp_data_source` | `config = jsonencode({...})`. Credentials are write-only (`secret_wo`, `provisioner_secret_wo`); change `credentials_wo_version` to send them again. | name |
| `fdp_schema` | Governs a schema or S3 namespace. Destroy stops governing it; the data is never touched. | ID |
| `fdp_app_group`, `fdp_app_group_member` | Groups that receive roles, and the directory identities bound to them. Users and IdP groups stay owned by SCIM. | ID; `<group>/<principal>` |
| `fdp_role_assignment` | A built-in role on any object, for an app group or one identity. The package repository is `resource_id = "packages"`: Publisher (publish new packages, import from PyPI/CRAN), Maintainer, Owner. | `<resource>/group/<id>` or `<resource>/principal/<id>` |
| `fdp_folder` | Organizes workloads; permissions are inherited. `branch` makes it an environment folder that only accepts that branch. | ID |
| `fdp_service_account` | A layer-bound execution identity (`run_as`). Destroy disables it; tokens stay runtime state. | name |
| `fdp_secret` | A group or service-account secret, or a certificate. The value and private key are write-only. | `<scope>/<owner>/<name>` |
| `fdp_repository` | Git credentials are write-only (`credentials_wo`). | name |
| `fdp_workload` | Deploys a program into a folder and layer; apply waits until it is live and reports the platform's reason if it fails. | ID |
| `fdp_api`, `fdp_api_operation` | Internal APIs (JWT/mTLS, or `anonymous = true` in `inbound` for testing outside production layers; backed by workloads in the same layer) and external APIs (outbound credentials as secret references). | ID; `<api>/<operation>` |
| `fdp_catalog_governance` | Documentation, steward, classification, tags and certification status of an object. Destroy resets it. | resource ID |
| `fdp_sla_policy` | Freshness, success rate, deadlines, input gates and enforcement. Destroy removes the policy. | resource ID |
| `fdp_metric` | A KPI or quality metric, optionally measured by a read-only SQL query. Every change is a new version; destroy disables it and keeps history. | `<resource>/<key>` |

Data sources: `fdp_layer` (by name) and `fdp_principal` (directory identity by name).

For JSON settings (`infrastructure`, `config`, `inbound`, `outbound`, `policy`), only the keys you set
are compared. Server defaults never show up as drift, but a change to a value you declared does,
and the next apply restores it.

Credentials never enter state: write-only attributes need OpenTofu 1.11 or later. After an import,
the API cannot say which credential version was last sent, so the first apply sends write-only
credentials once more.

Runs, logs, sessions, tokens and other runtime history are deliberately not managed here.

## Development

```sh
make test-terraform   # go vet, then create / re-plan / update / import / drift / guardrails / destroy for every resource
```

The acceptance test starts a temporary control plane (SQLite, in-memory secrets, fake
orchestrator) with a simulated OIDC issuer. `make test-e2e-install` exercises the provider
against a real Kubernetes installation.
