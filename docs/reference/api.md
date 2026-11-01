# HTTP API reference

Generated from FastAPI OpenAPI; do not edit manually.

Download the [OpenAPI specification](openapi.json). Paths below are relative to the public `/api` prefix. Authenticate with an IdP bearer token or an authorized FDP execution token. SCIM uses its separate provisioning credential. Internal endpoints are not a supported client API.

The installed server also exposes interactive OpenAPI at `/api/docs` when routed by its gateway. Use its schema to inspect request/response models for that installed revision.

## API management

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/apis` | List Apis |
| `POST` | `/v1/apis` | Create Api |
| `GET` | `/v1/apis/{api_id}` | Get Api |
| `PUT` | `/v1/apis/{api_id}` | Update Api |
| `DELETE` | `/v1/apis/{api_id}` | Delete Api |
| `GET` | `/v1/apis/{api_id}/calls` | List Calls |
| `POST` | `/v1/apis/{api_id}/operations` | Create Operation |
| `PUT` | `/v1/apis/{api_id}/operations/{op_id}` | Update Operation |
| `DELETE` | `/v1/apis/{api_id}/operations/{op_id}` | Delete Operation |
| `DELETE` | `/v1/egress/{slug}/{path}` | Egress |
| `GET` | `/v1/egress/{slug}/{path}` | Egress |
| `PATCH` | `/v1/egress/{slug}/{path}` | Egress |
| `POST` | `/v1/egress/{slug}/{path}` | Egress |
| `PUT` | `/v1/egress/{slug}/{path}` | Egress |

## RBAC

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/rbac/directory` | Directory |
| `POST` | `/v1/rbac/folders` | Create Folder |
| `DELETE` | `/v1/rbac/folders/{resource_id}` | Delete Folder |
| `GET` | `/v1/rbac/groups` | Groups |
| `POST` | `/v1/rbac/groups` | Create Group |
| `PUT` | `/v1/rbac/groups/{group_id}` | Update Group |
| `DELETE` | `/v1/rbac/groups/{group_id}` | Delete Group |
| `GET` | `/v1/rbac/groups/{group_id}/access` | Group Access |
| `GET` | `/v1/rbac/groups/{group_id}/assignments` | Group Assignments |
| `POST` | `/v1/rbac/groups/{group_id}/bindings` | Bind |
| `DELETE` | `/v1/rbac/groups/{group_id}/bindings/{principal_id}` | Unbind |
| `GET` | `/v1/rbac/principals/{principal_id}/access` | Principal Access |
| `GET` | `/v1/rbac/resources` | Resources |
| `PATCH` | `/v1/rbac/resources/{resource_id}` | Move Resource |
| `GET` | `/v1/rbac/resources/{resource_id}` | Read Resource |
| `DELETE` | `/v1/rbac/resources/{resource_id}` | Deregister Schema |
| `GET` | `/v1/rbac/resources/{resource_id}/access` | Effective Access |
| `GET` | `/v1/rbac/resources/{resource_id}/assignments` | Assignments |
| `PUT` | `/v1/rbac/resources/{resource_id}/assignments` | Assign |
| `DELETE` | `/v1/rbac/resources/{resource_id}/assignments/services/{principal_id}` | Revoke Service |
| `DELETE` | `/v1/rbac/resources/{resource_id}/assignments/{app_group_id}` | Revoke |
| `POST` | `/v1/rbac/resources/{resource_id}/provision` | Provision |
| `GET` | `/v1/rbac/resources/{resource_id}/service-principals` | Service Principals |
| `GET` | `/v1/rbac/resources/{resource_id}/user-principals` | User Principals |
| `GET` | `/v1/rbac/roles` | Roles |
| `POST` | `/v1/rbac/sources/{name}/schemas` | Register Schema |

## SCIM

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/scim/v2/ResourceTypes` | Resource Types |
| `GET` | `/scim/v2/ResourceTypes/{type_id}` | Resource Type |
| `GET` | `/scim/v2/Schemas` | Schemas |
| `GET` | `/scim/v2/Schemas/{schema_id}` | Schema |
| `GET` | `/scim/v2/ServiceProviderConfig` | Provider Config |
| `GET` | `/scim/v2/{collection}` | List Resources |
| `POST` | `/scim/v2/{collection}` | Create Resource |
| `GET` | `/scim/v2/{collection}/{pid}` | Get Resource |
| `PATCH` | `/scim/v2/{collection}/{pid}` | Update Resource |
| `PUT` | `/scim/v2/{collection}/{pid}` | Update Resource |
| `DELETE` | `/scim/v2/{collection}/{pid}` | Delete Resource |

## admin

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/admin/audit` | Audit Log |
| `GET` | `/v1/admin/datasources` | List Sources |
| `PUT` | `/v1/admin/datasources/{name}` | Upsert Source |
| `DELETE` | `/v1/admin/datasources/{name}` | Delete Source |
| `POST` | `/v1/admin/datasources/{name}/grants` | Grant |
| `DELETE` | `/v1/admin/datasources/{name}/grants/{group}` | Revoke |
| `GET` | `/v1/admin/pools` | List Pools |
| `PUT` | `/v1/admin/pools/{name}` | Upsert Pool |
| `DELETE` | `/v1/admin/pools/{name}` | Delete Pool |
| `GET` | `/v1/admin/settings/default-warehouse` | Default Warehouse Setting |
| `PUT` | `/v1/admin/settings/default-warehouse` | Set Default Warehouse |
| `DELETE` | `/v1/admin/settings/default-warehouse` | Clear Default Warehouse |
| `GET` | `/v1/admin/settings/egress` | Egress Allowlist |
| `PUT` | `/v1/admin/settings/egress` | Set Egress Allowlist |
| `GET` | `/v1/admin/settings/workspaces` | Workspace Settings |
| `PUT` | `/v1/admin/settings/workspaces` | Set Workspace Settings |
| `DELETE` | `/v1/admin/settings/workspaces` | Reset Workspace Settings |
| `GET` | `/v1/admin/storage-types` | Storage Type List |

## app profiles

| Method | Path | Purpose |
|---|---|---|
| `PUT` | `/v1/admin/app-profiles/{name}` | Put Profile |
| `DELETE` | `/v1/admin/app-profiles/{name}` | Delete Profile |
| `GET` | `/v1/app-profiles` | List Profiles |

## catalog

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/catalog/assets/{ident}` | Detail |
| `PUT` | `/v1/catalog/assets/{ident}/columns` | Put Columns |
| `PUT` | `/v1/catalog/assets/{ident}/governance` | Governance |
| `GET` | `/v1/catalog/assets/{ident}/lineage` | Lineage |
| `GET` | `/v1/catalog/assets/{ident}/product` | Asset Product |
| `PUT` | `/v1/catalog/assets/{ident}/sla` | Sla |
| `GET` | `/v1/catalog/classifications` | List Classifications |
| `PUT` | `/v1/catalog/classifications` | Put Classifications |
| `POST` | `/v1/catalog/ingest/airflow` | Ingest Airflow |
| `POST` | `/v1/catalog/ingest/dbt` | Ingest Dbt |
| `POST` | `/v1/catalog/ingest/program` | Ingest Program |
| `GET` | `/v1/catalog/products` | List Products |
| `POST` | `/v1/catalog/products` | Publish |
| `GET` | `/v1/catalog/products/candidates` | Publish Candidates |
| `PATCH` | `/v1/catalog/products/{product_id}` | Update Product |
| `DELETE` | `/v1/catalog/products/{product_id}` | Unpublish |
| `GET` | `/v1/catalog/products/{product_id}` | Product Detail |
| `PUT` | `/v1/catalog/products/{product_id}/consumers/{group_id}` | Put Consumer |
| `DELETE` | `/v1/catalog/products/{product_id}/consumers/{group_id}` | Delete Consumer |
| `POST` | `/v1/catalog/products/{product_id}/requests` | Request Access |
| `GET` | `/v1/catalog/requests` | List Requests |
| `DELETE` | `/v1/catalog/requests/{request_id}` | Withdraw |
| `POST` | `/v1/catalog/requests/{request_id}/decision` | Decide |
| `GET` | `/v1/catalog/resources/{resource_id}/asset` | Resource Asset |
| `POST` | `/v1/catalog/resources/{resource_id}/asset` | Ensure Asset |
| `POST` | `/v1/catalog/resources/{resource_id}/import/airflow` | Import Airflow |
| `POST` | `/v1/catalog/resources/{resource_id}/import/dbt` | Import Dbt |
| `GET` | `/v1/catalog/search` | Search |
| `GET` | `/v1/catalog/suggest` | Suggest |

## compliance

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/compliance/cases` | Create Case |
| `GET` | `/v1/compliance/cases` | List Cases |
| `GET` | `/v1/compliance/cases/{case_id}` | Get Case |
| `PATCH` | `/v1/compliance/cases/{case_id}` | Update Case |
| `POST` | `/v1/compliance/cases/{case_id}/comments` | Comment |
| `POST` | `/v1/compliance/cases/{case_id}/decision` | Decide Case |
| `POST` | `/v1/compliance/cases/{case_id}/files` | Add File |
| `GET` | `/v1/compliance/cases/{case_id}/files/{file_id}` | Get File |
| `POST` | `/v1/compliance/cases/{case_id}/renew` | Renew Case |
| `POST` | `/v1/compliance/cases/{case_id}/submit` | Submit Case |
| `POST` | `/v1/compliance/cases/{case_id}/withdraw` | Withdraw Case |
| `GET` | `/v1/compliance/deadlines` | List Deadlines |
| `GET` | `/v1/compliance/forms` | List Forms |
| `GET` | `/v1/compliance/forms/{form_id}` | Get Form |
| `DELETE` | `/v1/compliance/forms/{form_id}` | Delete Form |
| `POST` | `/v1/compliance/forms/{form_id}/publish` | Publish Form |
| `PUT` | `/v1/compliance/forms/{name}` | Put Form |
| `GET` | `/v1/compliance/memberships` | Pending Memberships |
| `POST` | `/v1/compliance/memberships/{group_id}/{principal_id}` | Decide Membership |
| `GET` | `/v1/compliance/policies` | List Policies |
| `PUT` | `/v1/compliance/policies/{layer_id}/{level}` | Put Policy |
| `DELETE` | `/v1/compliance/policies/{layer_id}/{level}` | Delete Policy |
| `GET` | `/v1/compliance/register` | Access Register |
| `POST` | `/v1/compliance/requirement` | Requirement |
| `GET` | `/v1/compliance/roles` | List Roles |
| `POST` | `/v1/compliance/roles` | Add Role |
| `DELETE` | `/v1/compliance/roles/{role_id}` | Remove Role |

## compute

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/compute/warehouses` | Warehouses |

## connect

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/connect` | Available |
| `GET` | `/v1/connect/{name}` | Connect |

## data browser

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/data/schemas/{resource_id}/children` | Children |
| `GET` | `/v1/data/schemas/{resource_id}/file` | File Preview |
| `GET` | `/v1/data/schemas/{resource_id}/preview` | Preview |

## data policies

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/data-policies` | List Policies |
| `POST` | `/v1/data-policies` | Create Policy |
| `POST` | `/v1/data-policies/normalize` | Normalize |
| `GET` | `/v1/data-policies/{policy_id}` | Get Policy |
| `PUT` | `/v1/data-policies/{policy_id}` | Update Policy |
| `DELETE` | `/v1/data-policies/{policy_id}` | Delete Policy |

## deploying

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/admin/repos` | Admin Repos |
| `PUT` | `/v1/admin/repos/{name}` | Upsert Repo |
| `DELETE` | `/v1/admin/repos/{name}` | Delete Repo |
| `POST` | `/v1/orchestrate/runs` | Orchestrate |
| `GET` | `/v1/repos` | List Repos |
| `GET` | `/v1/repos/{repo_id}/branches` | Repo Branches |
| `POST` | `/v1/repos/{repo_id}/pull` | Repo Pull |
| `GET` | `/v1/workloads` | Catalog |
| `POST` | `/v1/workloads` | Create Workload |
| `GET` | `/v1/workloads/{pub_id}` | Get Workload |
| `PATCH` | `/v1/workloads/{pub_id}` | Update Workload |
| `DELETE` | `/v1/workloads/{pub_id}` | Undeploy |
| `POST` | `/v1/workloads/{pub_id}/open` | Open Workload |
| `GET` | `/v1/workloads/{pub_id}/orchestrator-runs` | Orchestrator Runs |
| `POST` | `/v1/workloads/{pub_id}/runs` | Run Deployed |
| `POST` | `/v1/workloads/{pub_id}/sync` | Sync Now |
| `GET` | `/v1/workloads/{pub_id}/versions` | Workload Versions |

## identity

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/admin/runtimes` | Admin Runtimes |
| `PUT` | `/v1/admin/runtimes/{kind}/{version}` | Upsert Runtime |
| `DELETE` | `/v1/admin/runtimes/{kind}/{version}` | Delete Runtime |
| `PUT` | `/v1/admin/service-accounts/{name}` | Upsert Service Account |
| `DELETE` | `/v1/admin/service-accounts/{name}` | Delete Service Account |
| `GET` | `/v1/runtimes` | Runtimes |
| `GET` | `/v1/secrets` | List Secrets |
| `PUT` | `/v1/secrets/{scope}/{owner}/{name}` | Set Secret |
| `DELETE` | `/v1/secrets/{scope}/{owner}/{name}` | Delete Secret |
| `GET` | `/v1/service-accounts` | List Service Accounts |
| `GET` | `/v1/service-accounts/{name}/tokens` | List Tokens |
| `POST` | `/v1/service-accounts/{name}/tokens` | Create Token |
| `DELETE` | `/v1/service-accounts/{name}/tokens/{token_id}` | Revoke Token |

## kafka

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/kafka/clusters` | Clusters |
| `GET` | `/v1/kafka/clusters/{ident}` | Get Cluster |
| `DELETE` | `/v1/kafka/clusters/{ident}` | Unregister Cluster |
| `POST` | `/v1/kafka/clusters/{layer_id}` | Register Cluster |
| `GET` | `/v1/kafka/resolve` | Resolve Topic |
| `GET` | `/v1/kafka/topics` | Topics |
| `POST` | `/v1/kafka/topics` | Create Topic |
| `GET` | `/v1/kafka/topics/{ident}` | Get Topic |
| `PUT` | `/v1/kafka/topics/{ident}` | Update Topic |
| `DELETE` | `/v1/kafka/topics/{ident}` | Delete Topic |
| `GET` | `/v1/kafka/topics/{ident}/deletion` | Deletion |
| `POST` | `/v1/kafka/topics/{ident}/observations` | Observation |
| `POST` | `/v1/kafka/topics/{ident}/session` | Session |

## layers

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/admin/certificates` | Certificates |
| `POST` | `/v1/admin/layers` | Create |
| `PUT` | `/v1/admin/layers/{ident}` | Update |
| `DELETE` | `/v1/admin/layers/{ident}` | Remove |
| `GET` | `/v1/admin/layers/{ident}/infrastructure` | Infrastructure State |
| `PUT` | `/v1/admin/layers/{ident}/infrastructure` | Configure Infrastructure |
| `POST` | `/v1/admin/layers/{ident}/infrastructure/retire` | Retire Infrastructure |
| `POST` | `/v1/admin/layers/{ident}/infrastructure/retry` | Retry Infrastructure |
| `GET` | `/v1/admin/layers/{ident}/operations` | Infrastructure Operations |
| `GET` | `/v1/admin/settings/default-layer` | Default Layer |
| `PUT` | `/v1/admin/settings/default-layer` | Set Default Layer |
| `DELETE` | `/v1/admin/settings/default-layer` | Clear Default Layer |
| `GET` | `/v1/layers` | Listing |

## logs

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/logs` | Write Logs |
| `GET` | `/v1/logs` | Read Logs |

## meta

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/client-config` | Client Config |
| `GET` | `/v1/schema/fdp.json` | Fdp Schema |
| `GET` | `/v1/schema/layer-infrastructure.json` | Layer Infrastructure Schema |

## metrics and quality

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/catalog/alerts` | Alerts |
| `POST` | `/v1/catalog/alerts/{incident_id}/acknowledge` | Acknowledge |
| `POST` | `/v1/catalog/assets/{ident}/evaluate-sla` | Evaluate Sla |
| `GET` | `/v1/catalog/assets/{ident}/metrics` | Metrics |
| `POST` | `/v1/catalog/assets/{ident}/metrics` | Define |
| `GET` | `/v1/catalog/assets/{ident}/metrics/{metric_id}/history` | History |
| `POST` | `/v1/catalog/assets/{ident}/metrics/{metric_id}/measure` | Measure |
| `POST` | `/v1/catalog/assets/{ident}/metrics/{metric_id}/observations` | Report |
| `GET` | `/v1/catalog/assets/{ident}/metrics/{metric_id}/versions` | Versions |
| `POST` | `/v1/catalog/assets/{ident}/observations` | Dataset Report |
| `GET` | `/v1/catalog/assets/{ident}/sla-history` | Sla History |

## notifications

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/admin/webhooks` | List Webhooks |
| `PUT` | `/v1/admin/webhooks/{name}` | Put Webhook |
| `DELETE` | `/v1/admin/webhooks/{name}` | Delete Webhook |
| `GET` | `/v1/admin/webhooks/{name}/deliveries` | Deliveries |
| `POST` | `/v1/admin/webhooks/{name}/test` | Test Webhook |
| `GET` | `/v1/alerts/channels` | Alert Channels |
| `GET` | `/v1/alerts/events` | Alert Events |
| `GET` | `/v1/alerts/rules` | List Rules |
| `POST` | `/v1/alerts/rules` | Create Rule |
| `PUT` | `/v1/alerts/rules/{rule_id}` | Update Rule |
| `DELETE` | `/v1/alerts/rules/{rule_id}` | Delete Rule |

## observability

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/admin/settings/query-log` | Query Log Settings |
| `PUT` | `/v1/admin/settings/query-log` | Set Query Log Settings |
| `DELETE` | `/v1/admin/settings/query-log` | Reset Query Log Settings |
| `GET` | `/v1/admin/siem` | Siem Status |
| `GET` | `/v1/query-log` | List Queries |

## packages

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/packages` | List Packages |
| `GET` | `/v1/packages/allowlist` | Package Allowlist |
| `PUT` | `/v1/packages/allowlist` | Set Package Allowlist |
| `DELETE` | `/v1/packages/allowlist` | Reset Package Allowlist |
| `POST` | `/v1/packages/imports` | Request Import |
| `GET` | `/v1/packages/imports` | List Imports |
| `GET` | `/v1/packages/imports/{job_id}` | Get Import |
| `POST` | `/v1/packages/{kind}/upload` | Upload |
| `GET` | `/v1/packages/{kind}/{name}` | Get Package |
| `DELETE` | `/v1/packages/{kind}/{name}/versions/{version}` | Delete Version |
| `POST` | `/v1/packages/{kind}/{name}/versions/{version}/yank` | Yank |
| `DELETE` | `/v1/packages/{kind}/{name}/versions/{version}/yank` | Unyank |

## runs

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/runs` | List Runs |
| `POST` | `/v1/runs` | Create Run |
| `GET` | `/v1/runs/{run_id}` | Get Run |
| `GET` | `/v1/runs/{run_id}/logs` | Run Logs |
| `POST` | `/v1/runs/{run_id}/open` | Open App |
| `GET` | `/v1/runs/{run_id}/outputs` | Run Outputs |
| `GET` | `/v1/runs/{run_id}/outputs/{path}` | Run Output |
| `POST` | `/v1/runs/{run_id}/stop` | Stop Run |

## shared SQL

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/info` | Info |
| `GET` | `/v1/sql/pages/{token}` | Page |
| `DELETE` | `/v1/sql/pages/{token}` | Page |
| `GET` | `/v1/sql/queries/{query_id}` | Query Status |
| `POST` | `/v1/sql/query` | Query Request |
| `POST` | `/v1/statement` | Statement |

## viewer data access

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/delegated/files` | Files |
| `POST` | `/v1/delegated/query` | Query |

## workspaces

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/admin/workspace-templates` | List Templates |
| `POST` | `/v1/admin/workspace-templates` | Create Template |
| `GET` | `/v1/admin/workspace-templates/{template_id}` | Get Template |
| `PUT` | `/v1/admin/workspace-templates/{template_id}` | Update Template |
| `DELETE` | `/v1/admin/workspace-templates/{template_id}` | Delete Template |
| `POST` | `/v1/executions/token` | Refresh Run Token |
| `GET` | `/v1/me` | Me |
| `PUT` | `/v1/me/active-group` | Select Active Group |
| `PUT` | `/v1/me/active-layer` | Select Active Layer |
| `GET` | `/v1/me/git-credentials` | List Git Credentials |
| `POST` | `/v1/me/git-credentials` | Create Git Credential |
| `PUT` | `/v1/me/git-credentials/{credential_id}` | Update Git Credential |
| `DELETE` | `/v1/me/git-credentials/{credential_id}` | Delete Git Credential |
| `GET` | `/v1/pools` | List Pools |
| `GET` | `/v1/workspaces` | List Workspaces |
| `POST` | `/v1/workspaces` | Create Workspace |
| `DELETE` | `/v1/workspaces/{ws_id}` | Delete Workspace |
| `GET` | `/v1/workspaces/{ws_id}/config` | Workspace Config |
| `PUT` | `/v1/workspaces/{ws_id}/git` | Choose Workspace Git |
| `POST` | `/v1/workspaces/{ws_id}/open` | Open Workspace |
| `POST` | `/v1/workspaces/{ws_id}/start` | Start Workspace |
| `POST` | `/v1/workspaces/{ws_id}/stop` | Stop Workspace |
