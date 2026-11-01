# Route and page inventory

Inventory generated from local Python AST and desktop TypeScript declarations on 1 November 2026.
Companion to the [permission model plan](permission-model-plan.md). This lists entry points, not current authorization guarantees.
Every entry needs a specific action/target mapping in implementation; the plan groups the related functionality.

## HTTP entry points

Paths include router prefixes. Multi-method gateway routes are listed once with all declared methods.
Framework-generated OpenAPI/Swagger/ReDoc endpoints are covered separately below.

### admin

Storage connections; pools; workspace limits; network allowlist; audit.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/storage-types` | [`storage_type_list`](../services/control-plane/app/routes/admin.py#L35) |
| GET | `/v1/admin/datasources` | [`list_sources`](../services/control-plane/app/routes/admin.py#L41) |
| PUT | `/v1/admin/datasources/{name}` | [`upsert_source`](../services/control-plane/app/routes/admin.py#L52) |
| DELETE | `/v1/admin/datasources/{name}` | [`delete_source`](../services/control-plane/app/routes/admin.py#L159) |
| POST | `/v1/admin/datasources/{name}/grants` | [`grant`](../services/control-plane/app/routes/admin.py#L194) |
| DELETE | `/v1/admin/datasources/{name}/grants/{group}` | [`revoke`](../services/control-plane/app/routes/admin.py#L199) |
| GET | `/v1/admin/settings/workspaces` | [`workspace_settings`](../services/control-plane/app/routes/admin.py#L211) |
| PUT | `/v1/admin/settings/workspaces` | [`set_workspace_settings`](../services/control-plane/app/routes/admin.py#L218) |
| DELETE | `/v1/admin/settings/workspaces` | [`reset_workspace_settings`](../services/control-plane/app/routes/admin.py#L237) |
| GET | `/v1/admin/settings/default-warehouse` | [`default_warehouse_setting`](../services/control-plane/app/routes/admin.py#L262) |
| PUT | `/v1/admin/settings/default-warehouse` | [`set_default_warehouse`](../services/control-plane/app/routes/admin.py#L280) |
| DELETE | `/v1/admin/settings/default-warehouse` | [`clear_default_warehouse`](../services/control-plane/app/routes/admin.py#L315) |
| GET | `/v1/admin/settings/egress` | [`egress_allowlist`](../services/control-plane/app/routes/admin.py#L353) |
| PUT | `/v1/admin/settings/egress` | [`set_egress_allowlist`](../services/control-plane/app/routes/admin.py#L363) |
| GET | `/v1/admin/pools` | [`list_pools`](../services/control-plane/app/routes/admin.py#L431) |
| PUT | `/v1/admin/pools/{name}` | [`upsert_pool`](../services/control-plane/app/routes/admin.py#L437) |
| DELETE | `/v1/admin/pools/{name}` | [`delete_pool`](../services/control-plane/app/routes/admin.py#L473) |
| GET | `/v1/admin/audit` | [`audit_log`](../services/control-plane/app/routes/admin.py#L497) |

### apis

API definitions; operations; calls; inbound/outbound execution.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/apis` | [`list_apis`](../services/control-plane/app/routes/apis.py#L116) |
| POST | `/v1/apis` | [`create_api`](../services/control-plane/app/routes/apis.py#L153) |
| GET | `/v1/apis/{api_id}` | [`get_api`](../services/control-plane/app/routes/apis.py#L175) |
| PUT | `/v1/apis/{api_id}` | [`update_api`](../services/control-plane/app/routes/apis.py#L186) |
| DELETE | `/v1/apis/{api_id}` | [`delete_api`](../services/control-plane/app/routes/apis.py#L201) |
| POST | `/v1/apis/{api_id}/operations` | [`create_operation`](../services/control-plane/app/routes/apis.py#L280) |
| PUT | `/v1/apis/{api_id}/operations/{op_id}` | [`update_operation`](../services/control-plane/app/routes/apis.py#L303) |
| DELETE | `/v1/apis/{api_id}/operations/{op_id}` | [`delete_operation`](../services/control-plane/app/routes/apis.py#L326) |
| GET | `/v1/apis/{api_id}/calls` | [`list_calls`](../services/control-plane/app/routes/apis.py#L342) |
| GET, POST, PUT, PATCH, DELETE | `/gw/{slug}` | [`inbound`](../services/control-plane/app/routes/apis.py#L500) |
| GET, POST, PUT, PATCH, DELETE | `/gw/{slug}/{path:path}` | [`inbound`](../services/control-plane/app/routes/apis.py#L500) |
| GET, POST, PUT, PATCH, DELETE | `/v1/egress/{slug}/{path:path}` | [`egress`](../services/control-plane/app/routes/apis.py#L511) |

### app_profiles

App profiles.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/app-profiles` | [`list_profiles`](../services/control-plane/app/routes/app_profiles.py#L39) |
| PUT | `/v1/admin/app-profiles/{name}` | [`put_profile`](../services/control-plane/app/routes/app_profiles.py#L46) |
| DELETE | `/v1/admin/app-profiles/{name}` | [`delete_profile`](../services/control-plane/app/routes/app_profiles.py#L70) |

### catalog

Catalog; metadata; lineage; classifications.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/catalog/search` | [`search`](../services/control-plane/app/routes/catalog.py#L145) |
| GET | `/v1/catalog/suggest` | [`suggest`](../services/control-plane/app/routes/catalog.py#L197) |
| GET | `/v1/catalog/resources/{resource_id}/asset` | [`resource_asset`](../services/control-plane/app/routes/catalog.py#L206) |
| GET | `/v1/catalog/assets/{ident}` | [`detail`](../services/control-plane/app/routes/catalog.py#L222) |
| PUT | `/v1/catalog/assets/{ident}/governance` | [`governance`](../services/control-plane/app/routes/catalog.py#L232) |
| PUT | `/v1/catalog/assets/{ident}/sla` | [`sla`](../services/control-plane/app/routes/catalog.py#L266) |
| GET | `/v1/catalog/assets/{ident}/lineage` | [`lineage`](../services/control-plane/app/routes/catalog.py#L290) |
| POST | `/v1/catalog/resources/{resource_id}/import/dbt` | [`import_dbt`](../services/control-plane/app/routes/catalog.py#L314) |
| POST | `/v1/catalog/resources/{resource_id}/import/airflow` | [`import_airflow`](../services/control-plane/app/routes/catalog.py#L325) |
| POST | `/v1/catalog/ingest/dbt` | [`ingest_dbt`](../services/control-plane/app/routes/catalog.py#L346) |
| POST | `/v1/catalog/ingest/airflow` | [`ingest_airflow`](../services/control-plane/app/routes/catalog.py#L357) |
| POST | `/v1/catalog/ingest/program` | [`ingest_program`](../services/control-plane/app/routes/catalog.py#L364) |
| GET | `/v1/catalog/classifications` | [`list_classifications`](../services/control-plane/app/routes/catalog.py#L399) |
| PUT | `/v1/catalog/classifications` | [`put_classifications`](../services/control-plane/app/routes/catalog.py#L418) |
| PUT | `/v1/catalog/assets/{ident}/columns` | [`put_columns`](../services/control-plane/app/routes/catalog.py#L435) |
| POST | `/v1/catalog/resources/{resource_id}/asset` | [`ensure_asset`](../services/control-plane/app/routes/catalog.py#L463) |

### compliance

Forms; layer access policies; review duties; cases; membership; register; deadlines.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/compliance/forms` | [`list_forms`](../services/control-plane/app/routes/compliance.py#L52) |
| GET | `/v1/compliance/forms/{form_id}` | [`get_form`](../services/control-plane/app/routes/compliance.py#L58) |
| PUT | `/v1/compliance/forms/{name}` | [`put_form`](../services/control-plane/app/routes/compliance.py#L67) |
| POST | `/v1/compliance/forms/{form_id}/publish` | [`publish_form`](../services/control-plane/app/routes/compliance.py#L91) |
| DELETE | `/v1/compliance/forms/{form_id}` | [`delete_form`](../services/control-plane/app/routes/compliance.py#L104) |
| GET | `/v1/compliance/policies` | [`list_policies`](../services/control-plane/app/routes/compliance.py#L148) |
| PUT | `/v1/compliance/policies/{layer_id}/{level}` | [`put_policy`](../services/control-plane/app/routes/compliance.py#L160) |
| DELETE | `/v1/compliance/policies/{layer_id}/{level}` | [`delete_policy`](../services/control-plane/app/routes/compliance.py#L190) |
| GET | `/v1/compliance/roles` | [`list_roles`](../services/control-plane/app/routes/compliance.py#L223) |
| POST | `/v1/compliance/roles` | [`add_role`](../services/control-plane/app/routes/compliance.py#L230) |
| DELETE | `/v1/compliance/roles/{role_id}` | [`remove_role`](../services/control-plane/app/routes/compliance.py#L250) |
| POST | `/v1/compliance/requirement` | [`requirement`](../services/control-plane/app/routes/compliance.py#L294) |
| POST | `/v1/compliance/cases` | [`create_case`](../services/control-plane/app/routes/compliance.py#L387) |
| GET | `/v1/compliance/cases` | [`list_cases`](../services/control-plane/app/routes/compliance.py#L407) |
| GET | `/v1/compliance/cases/{case_id}` | [`get_case`](../services/control-plane/app/routes/compliance.py#L427) |
| PATCH | `/v1/compliance/cases/{case_id}` | [`update_case`](../services/control-plane/app/routes/compliance.py#L438) |
| POST | `/v1/compliance/cases/{case_id}/submit` | [`submit_case`](../services/control-plane/app/routes/compliance.py#L454) |
| POST | `/v1/compliance/cases/{case_id}/decision` | [`decide_case`](../services/control-plane/app/routes/compliance.py#L479) |
| POST | `/v1/compliance/cases/{case_id}/comments` | [`comment`](../services/control-plane/app/routes/compliance.py#L496) |
| POST | `/v1/compliance/cases/{case_id}/withdraw` | [`withdraw_case`](../services/control-plane/app/routes/compliance.py#L506) |
| POST | `/v1/compliance/cases/{case_id}/files` | [`add_file`](../services/control-plane/app/routes/compliance.py#L524) |
| GET | `/v1/compliance/cases/{case_id}/files/{file_id}` | [`get_file`](../services/control-plane/app/routes/compliance.py#L548) |
| GET | `/v1/compliance/memberships` | [`pending_memberships`](../services/control-plane/app/routes/compliance.py#L560) |
| POST | `/v1/compliance/memberships/{group_id}/{principal_id}` | [`decide_membership`](../services/control-plane/app/routes/compliance.py#L582) |
| GET | `/v1/compliance/register` | [`access_register`](../services/control-plane/app/routes/compliance.py#L635) |
| POST | `/v1/compliance/cases/{case_id}/renew` | [`renew_case`](../services/control-plane/app/routes/compliance.py#L678) |
| GET | `/v1/compliance/deadlines` | [`list_deadlines`](../services/control-plane/app/routes/compliance.py#L695) |

### compute

Usage / compute occupancy.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/compute/warehouses` | [`warehouses`](../services/control-plane/app/routes/compute.py#L102) |

### connect

Connection and credential issuance.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/connect` | [`available`](../services/control-plane/app/routes/connect.py#L23) |
| GET | `/v1/connect/{name}` | [`connect`](../services/control-plane/app/routes/connect.py#L29) |

### data_browser

Storage metadata; preview; files.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/data/schemas/{resource_id}/preview` | [`preview`](../services/control-plane/app/routes/data_browser.py#L19) |
| GET | `/v1/data/schemas/{resource_id}/children` | [`children`](../services/control-plane/app/routes/data_browser.py#L125) |
| GET | `/v1/data/schemas/{resource_id}/file` | [`file_preview`](../services/control-plane/app/routes/data_browser.py#L190) |

### data_policies

Row filters and column masks.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/data-policies` | [`list_policies`](../services/control-plane/app/routes/data_policies.py#L102) |
| POST | `/v1/data-policies/normalize` | [`normalize`](../services/control-plane/app/routes/data_policies.py#L122) |
| GET | `/v1/data-policies/{policy_id}` | [`get_policy`](../services/control-plane/app/routes/data_policies.py#L129) |
| POST | `/v1/data-policies` | [`create_policy`](../services/control-plane/app/routes/data_policies.py#L137) |
| PUT | `/v1/data-policies/{policy_id}` | [`update_policy`](../services/control-plane/app/routes/data_policies.py#L159) |
| DELETE | `/v1/data-policies/{policy_id}` | [`delete_policy`](../services/control-plane/app/routes/data_policies.py#L175) |

### delegation

Caller-bound SQL and files.

| Method | Path | Handler / source |
|---|---|---|
| POST | `/v1/delegated/query` | [`query`](../services/control-plane/app/routes/delegation.py#L27) |
| POST | `/v1/delegated/files` | [`files`](../services/control-plane/app/routes/delegation.py#L97) |

### deploy

Repositories; workloads; deployment; execution.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/repos` | [`admin_repos`](../services/control-plane/app/routes/deploy.py#L55) |
| PUT | `/v1/admin/repos/{name}` | [`upsert_repo`](../services/control-plane/app/routes/deploy.py#L61) |
| DELETE | `/v1/admin/repos/{name}` | [`delete_repo`](../services/control-plane/app/routes/deploy.py#L86) |
| GET | `/v1/repos` | [`list_repos`](../services/control-plane/app/routes/deploy.py#L100) |
| GET | `/v1/repos/{repo_id}/branches` | [`repo_branches`](../services/control-plane/app/routes/deploy.py#L108) |
| POST | `/v1/repos/{repo_id}/pull` | [`repo_pull`](../services/control-plane/app/routes/deploy.py#L120) |
| GET | `/v1/workloads` | [`catalog`](../services/control-plane/app/routes/deploy.py#L193) |
| POST | `/v1/workloads` | [`create_workload`](../services/control-plane/app/routes/deploy.py#L201) |
| GET | `/v1/workloads/{pub_id}` | [`get_workload`](../services/control-plane/app/routes/deploy.py#L287) |
| GET | `/v1/workloads/{pub_id}/versions` | [`workload_versions`](../services/control-plane/app/routes/deploy.py#L292) |
| PATCH | `/v1/workloads/{pub_id}` | [`update_workload`](../services/control-plane/app/routes/deploy.py#L339) |
| POST | `/v1/workloads/{pub_id}/sync` | [`sync_now`](../services/control-plane/app/routes/deploy.py#L396) |
| DELETE | `/v1/workloads/{pub_id}` | [`undeploy`](../services/control-plane/app/routes/deploy.py#L410) |
| POST | `/v1/workloads/{pub_id}/open` | [`open_workload`](../services/control-plane/app/routes/deploy.py#L427) |
| GET | `/v1/workloads/{pub_id}/orchestrator-runs` | [`orchestrator_runs`](../services/control-plane/app/routes/deploy.py#L437) |
| POST | `/v1/workloads/{pub_id}/runs` | [`run_deployed`](../services/control-plane/app/routes/deploy.py#L454) |
| POST | `/v1/orchestrate/runs` | [`orchestrate`](../services/control-plane/app/routes/deploy.py#L492) |
| GET | `/v1/workloads/{pub_id}/source` | [`source`](../services/control-plane/app/routes/deploy.py#L549) |

### gateway

Gateway authentication/routing; app/editor/docs admission; metrics.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/internal/metrics` | [`platform_metrics`](../services/control-plane/app/routes/gateway.py#L51) |
| GET | `/internal/metrics/certificates` | [`certificate_metrics`](../services/control-plane/app/routes/gateway.py#L66) |
| GET | `/internal/egress/allowlist` | [`egress_allowlist`](../services/control-plane/app/routes/gateway.py#L79) |
| POST | `/internal/egress/authorize` | [`egress_authorize`](../services/control-plane/app/routes/gateway.py#L94) |
| GET | `/internal/traefik/config` | [`traefik_config`](../services/control-plane/app/routes/gateway.py#L127) |
| GET | `/internal/auth/forward` | [`forward_auth`](../services/control-plane/app/routes/gateway.py#L246) |
| GET | `/ws/{ws_id}/{path:path}` | [`workspace_waiting`](../services/control-plane/app/routes/gateway.py#L339) |
| GET | `/p/{slug}/` | [`dbt_docs`](../services/control-plane/app/routes/gateway.py#L366) |
| GET | `/p/{slug}/index.html` | [`dbt_docs`](../services/control-plane/app/routes/gateway.py#L366) |

### git_credentials

Personal Git credentials and workspace bindings.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/me/git-credentials` | [`list_git_credentials`](../services/control-plane/app/routes/git_credentials.py#L76) |
| POST | `/v1/me/git-credentials` | [`create_git_credential`](../services/control-plane/app/routes/git_credentials.py#L82) |
| PUT | `/v1/me/git-credentials/{credential_id}` | [`update_git_credential`](../services/control-plane/app/routes/git_credentials.py#L94) |
| DELETE | `/v1/me/git-credentials/{credential_id}` | [`delete_git_credential`](../services/control-plane/app/routes/git_credentials.py#L104) |
| PUT | `/v1/workspaces/{ws_id}/git` | [`choose_workspace_git`](../services/control-plane/app/routes/git_credentials.py#L121) |
| GET | `/v1/workspaces/{ws_id}/git-credential` | [`workspace_git_credential`](../services/control-plane/app/routes/git_credentials.py#L137) |

### identity

Runtimes; secrets; certificates; service identities; tokens.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/runtimes` | [`runtimes`](../services/control-plane/app/routes/identity.py#L57) |
| GET | `/v1/admin/runtimes` | [`admin_runtimes`](../services/control-plane/app/routes/identity.py#L63) |
| PUT | `/v1/admin/runtimes/{kind}/{version}` | [`upsert_runtime`](../services/control-plane/app/routes/identity.py#L68) |
| DELETE | `/v1/admin/runtimes/{kind}/{version}` | [`delete_runtime`](../services/control-plane/app/routes/identity.py#L98) |
| GET | `/v1/secrets` | [`list_secrets`](../services/control-plane/app/routes/identity.py#L157) |
| PUT | `/v1/secrets/{scope}/{owner}/{name}` | [`set_secret`](../services/control-plane/app/routes/identity.py#L167) |
| DELETE | `/v1/secrets/{scope}/{owner}/{name}` | [`delete_secret`](../services/control-plane/app/routes/identity.py#L190) |
| GET | `/v1/service-accounts` | [`list_service_accounts`](../services/control-plane/app/routes/identity.py#L280) |
| PUT | `/v1/admin/service-accounts/{name}` | [`upsert_service_account`](../services/control-plane/app/routes/identity.py#L287) |
| DELETE | `/v1/admin/service-accounts/{name}` | [`delete_service_account`](../services/control-plane/app/routes/identity.py#L333) |
| GET | `/v1/service-accounts/{name}/tokens` | [`list_tokens`](../services/control-plane/app/routes/identity.py#L349) |
| POST | `/v1/service-accounts/{name}/tokens` | [`create_token`](../services/control-plane/app/routes/identity.py#L357) |
| DELETE | `/v1/service-accounts/{name}/tokens/{token_id}` | [`revoke_token`](../services/control-plane/app/routes/identity.py#L373) |

### kafka

Clusters; topics; sessions; broker authorization.

| Method | Path | Handler / source |
|---|---|---|
| POST | `/v1/kafka/clusters/{layer_id}` | [`register_cluster`](../services/control-plane/app/routes/kafka.py#L40) |
| GET | `/v1/kafka/clusters` | [`clusters`](../services/control-plane/app/routes/kafka.py#L62) |
| GET | `/v1/kafka/topics` | [`topics`](../services/control-plane/app/routes/kafka.py#L68) |
| GET | `/v1/kafka/clusters/{ident}` | [`get_cluster`](../services/control-plane/app/routes/kafka.py#L75) |
| DELETE | `/v1/kafka/clusters/{ident}` | [`unregister_cluster`](../services/control-plane/app/routes/kafka.py#L92) |
| GET | `/v1/kafka/resolve` | [`resolve_topic`](../services/control-plane/app/routes/kafka.py#L109) |
| GET | `/v1/kafka/topics/{ident}` | [`get_topic`](../services/control-plane/app/routes/kafka.py#L120) |
| POST | `/v1/kafka/topics` | [`create_topic`](../services/control-plane/app/routes/kafka.py#L127) |
| PUT | `/v1/kafka/topics/{ident}` | [`update_topic`](../services/control-plane/app/routes/kafka.py#L162) |
| DELETE | `/v1/kafka/topics/{ident}` | [`delete_topic`](../services/control-plane/app/routes/kafka.py#L177) |
| GET | `/v1/kafka/topics/{ident}/deletion` | [`deletion`](../services/control-plane/app/routes/kafka.py#L187) |
| POST | `/v1/kafka/topics/{ident}/session` | [`session`](../services/control-plane/app/routes/kafka.py#L196) |
| POST | `/v1/kafka/topics/{ident}/observations` | [`observation`](../services/control-plane/app/routes/kafka.py#L205) |
| GET | `/internal/kafka/jwks` | [`jwks`](../services/control-plane/app/routes/kafka.py#L236) |
| POST | `/internal/kafka/layers/{layer_id}/allow` | [`allow`](../services/control-plane/app/routes/kafka.py#L242) |

### layers

Layers; defaults; infrastructure lifecycle; certificate inventory.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/layers/{ident}/infrastructure` | [`infrastructure_state`](../services/control-plane/app/routes/layers.py#L20) |
| GET | `/v1/admin/certificates` | [`certificates`](../services/control-plane/app/routes/layers.py#L28) |
| PUT | `/v1/admin/layers/{ident}/infrastructure` | [`configure_infrastructure`](../services/control-plane/app/routes/layers.py#L37) |
| POST | `/v1/admin/layers/{ident}/infrastructure/retry` | [`retry_infrastructure`](../services/control-plane/app/routes/layers.py#L54) |
| POST | `/v1/admin/layers/{ident}/infrastructure/retire` | [`retire_infrastructure`](../services/control-plane/app/routes/layers.py#L73) |
| GET | `/v1/admin/layers/{ident}/operations` | [`infrastructure_operations`](../services/control-plane/app/routes/layers.py#L94) |
| GET | `/v1/layers` | [`listing`](../services/control-plane/app/routes/layers.py#L123) |
| GET | `/v1/admin/settings/default-layer` | [`default_layer`](../services/control-plane/app/routes/layers.py#L131) |
| PUT | `/v1/admin/settings/default-layer` | [`set_default_layer`](../services/control-plane/app/routes/layers.py#L140) |
| DELETE | `/v1/admin/settings/default-layer` | [`clear_default_layer`](../services/control-plane/app/routes/layers.py#L155) |
| POST | `/v1/admin/layers` | [`create`](../services/control-plane/app/routes/layers.py#L166) |
| PUT | `/v1/admin/layers/{ident}` | [`update`](../services/control-plane/app/routes/layers.py#L190) |
| DELETE | `/v1/admin/layers/{ident}` | [`remove`](../services/control-plane/app/routes/layers.py#L209) |

### logs

Log ingestion and scoped reading.

| Method | Path | Handler / source |
|---|---|---|
| POST | `/v1/logs` | [`write_logs`](../services/control-plane/app/routes/logs.py#L78) |
| GET | `/v1/logs` | [`read_logs`](../services/control-plane/app/routes/logs.py#L149) |

### measurements

Metrics; observations; SLAs; incidents.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/catalog/assets/{ident}/metrics` | [`metrics`](../services/control-plane/app/routes/measurements.py#L89) |
| POST | `/v1/catalog/assets/{ident}/metrics` | [`define`](../services/control-plane/app/routes/measurements.py#L96) |
| GET | `/v1/catalog/assets/{ident}/metrics/{metric_id}/history` | [`history`](../services/control-plane/app/routes/measurements.py#L125) |
| GET | `/v1/catalog/assets/{ident}/metrics/{metric_id}/versions` | [`versions`](../services/control-plane/app/routes/measurements.py#L132) |
| POST | `/v1/catalog/assets/{ident}/metrics/{metric_id}/observations` | [`report`](../services/control-plane/app/routes/measurements.py#L145) |
| POST | `/v1/catalog/assets/{ident}/metrics/{metric_id}/measure` | [`measure`](../services/control-plane/app/routes/measurements.py#L163) |
| POST | `/v1/catalog/assets/{ident}/observations` | [`dataset_report`](../services/control-plane/app/routes/measurements.py#L205) |
| GET | `/v1/catalog/assets/{ident}/sla-history` | [`sla_history`](../services/control-plane/app/routes/measurements.py#L222) |
| POST | `/v1/catalog/assets/{ident}/evaluate-sla` | [`evaluate_sla`](../services/control-plane/app/routes/measurements.py#L229) |
| GET | `/v1/catalog/alerts` | [`alerts`](../services/control-plane/app/routes/measurements.py#L238) |
| POST | `/v1/catalog/alerts/{incident_id}/acknowledge` | [`acknowledge`](../services/control-plane/app/routes/measurements.py#L258) |

### packages

Packages; publication; imports; policy; Python/R feeds.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/packages` | [`list_packages`](../services/control-plane/app/routes/packages.py#L139) |
| GET | `/v1/packages/allowlist` | [`package_allowlist`](../services/control-plane/app/routes/packages.py#L166) |
| PUT | `/v1/packages/allowlist` | [`set_package_allowlist`](../services/control-plane/app/routes/packages.py#L173) |
| DELETE | `/v1/packages/allowlist` | [`reset_package_allowlist`](../services/control-plane/app/routes/packages.py#L198) |
| POST | `/v1/packages/imports` | [`request_import`](../services/control-plane/app/routes/packages.py#L217) |
| GET | `/v1/packages/imports` | [`list_imports`](../services/control-plane/app/routes/packages.py#L236) |
| GET | `/v1/packages/imports/{job_id}` | [`get_import`](../services/control-plane/app/routes/packages.py#L241) |
| POST | `/v1/packages/{kind}/upload` | [`upload`](../services/control-plane/app/routes/packages.py#L263) |
| GET | `/v1/packages/{kind}/{name}` | [`get_package`](../services/control-plane/app/routes/packages.py#L271) |
| POST | `/v1/packages/{kind}/{name}/versions/{version}/yank` | [`yank`](../services/control-plane/app/routes/packages.py#L285) |
| DELETE | `/v1/packages/{kind}/{name}/versions/{version}/yank` | [`unyank`](../services/control-plane/app/routes/packages.py#L299) |
| DELETE | `/v1/packages/{kind}/{name}/versions/{version}` | [`delete_version`](../services/control-plane/app/routes/packages.py#L312) |
| GET | `/v1/packages/python/simple/` | [`simple_index`](../services/control-plane/app/routes/packages.py#L341) |
| GET | `/v1/packages/python/simple/{name}/` | [`simple_project`](../services/control-plane/app/routes/packages.py#L348) |
| GET | `/v1/packages/python/files/{package_key}/{filename}` | [`python_file`](../services/control-plane/app/routes/packages.py#L365) |
| POST | `/v1/packages/python/legacy/` | [`twine_upload`](../services/control-plane/app/routes/packages.py#L374) |
| GET | `/v1/packages/r/src/contrib/PACKAGES` | [`r_packages`](../services/control-plane/app/routes/packages.py#L390) |
| GET | `/v1/packages/r/src/contrib/PACKAGES.gz` | [`r_packages_gz`](../services/control-plane/app/routes/packages.py#L395) |
| GET | `/v1/packages/r/src/contrib/Archive/{name}/{filename}` | [`r_archive_file`](../services/control-plane/app/routes/packages.py#L400) |
| GET | `/v1/packages/r/src/contrib/{filename}` | [`r_source_file`](../services/control-plane/app/routes/packages.py#L408) |

### permissions

Named permission assignments; capability discovery; custom roles.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/permissions/registry` | [`registry`](../services/control-plane/app/routes/permissions.py#L74) |
| GET | `/v1/permissions/capabilities` | [`capabilities`](../services/control-plane/app/routes/permissions.py#L79) |
| POST | `/v1/permissions/assignments` | [`create`](../services/control-plane/app/routes/permissions.py#L84) |
| GET | `/v1/permissions/assignments/{ident}` | [`read`](../services/control-plane/app/routes/permissions.py#L104) |
| PUT | `/v1/permissions/assignments/{ident}` | [`update`](../services/control-plane/app/routes/permissions.py#L109) |
| DELETE | `/v1/permissions/assignments/{ident}` | [`remove`](../services/control-plane/app/routes/permissions.py#L121) |
| POST | `/v1/permissions/roles` | [`create_role`](../services/control-plane/app/routes/permissions.py#L145) |
| GET | `/v1/permissions/roles/{ident}` | [`read_role`](../services/control-plane/app/routes/permissions.py#L158) |
| PUT | `/v1/permissions/roles/{ident}` | [`update_role`](../services/control-plane/app/routes/permissions.py#L167) |
| DELETE | `/v1/permissions/roles/{ident}` | [`remove_role`](../services/control-plane/app/routes/permissions.py#L187) |

### products

Products; access options; requests; consumer grants.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/catalog/products` | [`list_products`](../services/control-plane/app/routes/products.py#L254) |
| GET | `/v1/catalog/products/candidates` | [`publish_candidates`](../services/control-plane/app/routes/products.py#L274) |
| GET | `/v1/catalog/assets/{ident}/product` | [`asset_product`](../services/control-plane/app/routes/products.py#L310) |
| POST | `/v1/catalog/products` | [`publish`](../services/control-plane/app/routes/products.py#L423) |
| PATCH | `/v1/catalog/products/{product_id}` | [`update_product`](../services/control-plane/app/routes/products.py#L470) |
| DELETE | `/v1/catalog/products/{product_id}` | [`unpublish`](../services/control-plane/app/routes/products.py#L499) |
| GET | `/v1/catalog/products/{product_id}` | [`product_detail`](../services/control-plane/app/routes/products.py#L523) |
| POST | `/v1/catalog/products/{product_id}/requests` | [`request_access`](../services/control-plane/app/routes/products.py#L640) |
| GET | `/v1/catalog/requests` | [`list_requests`](../services/control-plane/app/routes/products.py#L683) |
| POST | `/v1/catalog/requests/{request_id}/decision` | [`decide`](../services/control-plane/app/routes/products.py#L701) |
| DELETE | `/v1/catalog/requests/{request_id}` | [`withdraw`](../services/control-plane/app/routes/products.py#L749) |
| PUT | `/v1/catalog/products/{product_id}/consumers/{group_id}` | [`put_consumer`](../services/control-plane/app/routes/products.py#L761) |
| DELETE | `/v1/catalog/products/{product_id}/consumers/{group_id}` | [`delete_consumer`](../services/control-plane/app/routes/products.py#L792) |

### query_log

Query log; retention; SIEM; engine events.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/siem` | [`siem_status`](../services/control-plane/app/routes/query_log.py#L25) |
| POST | `/internal/trino/layers/{layer_id}/events` | [`trino_events`](../services/control-plane/app/routes/query_log.py#L33) |
| GET | `/v1/query-log` | [`list_queries`](../services/control-plane/app/routes/query_log.py#L55) |
| GET | `/v1/admin/settings/query-log` | [`query_log_settings`](../services/control-plane/app/routes/query_log.py#L101) |
| PUT | `/v1/admin/settings/query-log` | [`set_query_log_settings`](../services/control-plane/app/routes/query_log.py#L106) |
| DELETE | `/v1/admin/settings/query-log` | [`reset_query_log_settings`](../services/control-plane/app/routes/query_log.py#L114) |

### rbac

People/groups; grants; folders; schemas; provisioning.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/rbac/roles` | [`roles`](../services/control-plane/app/routes/rbac.py#L120) |
| GET | `/v1/rbac/directory` | [`directory`](../services/control-plane/app/routes/rbac.py#L129) |
| GET | `/v1/rbac/groups` | [`groups`](../services/control-plane/app/routes/rbac.py#L163) |
| GET | `/v1/rbac/groups/{group_id}/assignments` | [`group_assignments`](../services/control-plane/app/routes/rbac.py#L177) |
| GET | `/v1/rbac/groups/{group_id}/access` | [`group_access`](../services/control-plane/app/routes/rbac.py#L234) |
| GET | `/v1/rbac/principals/{principal_id}/access` | [`principal_access`](../services/control-plane/app/routes/rbac.py#L251) |
| POST | `/v1/rbac/groups` | [`create_group`](../services/control-plane/app/routes/rbac.py#L289) |
| PUT | `/v1/rbac/groups/{group_id}` | [`update_group`](../services/control-plane/app/routes/rbac.py#L309) |
| DELETE | `/v1/rbac/groups/{group_id}` | [`delete_group`](../services/control-plane/app/routes/rbac.py#L332) |
| POST | `/v1/rbac/groups/{group_id}/bindings` | [`bind`](../services/control-plane/app/routes/rbac.py#L370) |
| DELETE | `/v1/rbac/groups/{group_id}/bindings/{principal_id}` | [`unbind`](../services/control-plane/app/routes/rbac.py#L387) |
| GET | `/v1/rbac/resources` | [`resources`](../services/control-plane/app/routes/rbac.py#L399) |
| POST | `/v1/rbac/folders` | [`create_folder`](../services/control-plane/app/routes/rbac.py#L429) |
| PATCH | `/v1/rbac/resources/{resource_id}` | [`move_resource`](../services/control-plane/app/routes/rbac.py#L459) |
| DELETE | `/v1/rbac/folders/{resource_id}` | [`delete_folder`](../services/control-plane/app/routes/rbac.py#L497) |
| POST | `/v1/rbac/sources/{name}/schemas` | [`register_schema`](../services/control-plane/app/routes/rbac.py#L524) |
| GET | `/v1/rbac/resources/{resource_id}` | [`read_resource`](../services/control-plane/app/routes/rbac.py#L560) |
| DELETE | `/v1/rbac/resources/{resource_id}` | [`deregister_schema`](../services/control-plane/app/routes/rbac.py#L571) |
| GET | `/v1/rbac/resources/{resource_id}/assignments` | [`assignments`](../services/control-plane/app/routes/rbac.py#L585) |
| GET | `/v1/rbac/resources/{resource_id}/service-principals` | [`service_principals`](../services/control-plane/app/routes/rbac.py#L608) |
| GET | `/v1/rbac/resources/{resource_id}/user-principals` | [`user_principals`](../services/control-plane/app/routes/rbac.py#L619) |
| PUT | `/v1/rbac/resources/{resource_id}/assignments` | [`assign`](../services/control-plane/app/routes/rbac.py#L650) |
| DELETE | `/v1/rbac/resources/{resource_id}/assignments/{app_group_id}` | [`revoke`](../services/control-plane/app/routes/rbac.py#L685) |
| DELETE | `/v1/rbac/resources/{resource_id}/assignments/services/{principal_id}` | [`revoke_service`](../services/control-plane/app/routes/rbac.py#L697) |
| GET | `/v1/rbac/resources/{resource_id}/access` | [`effective_access`](../services/control-plane/app/routes/rbac.py#L707) |
| POST | `/v1/rbac/resources/{resource_id}/provision` | [`provision`](../services/control-plane/app/routes/rbac.py#L714) |

### runs

Run admission; logs; outputs; cancellation; app admission.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/runs` | [`list_runs`](../services/control-plane/app/routes/runs.py#L61) |
| POST | `/v1/runs` | [`create_run`](../services/control-plane/app/routes/runs.py#L281) |
| GET | `/v1/runs/{run_id}` | [`get_run`](../services/control-plane/app/routes/runs.py#L304) |
| GET | `/v1/runs/{run_id}/logs` | [`run_logs`](../services/control-plane/app/routes/runs.py#L309) |
| GET | `/v1/runs/{run_id}/outputs` | [`run_outputs`](../services/control-plane/app/routes/runs.py#L324) |
| GET | `/v1/runs/{run_id}/outputs/{path:path}` | [`run_output`](../services/control-plane/app/routes/runs.py#L339) |
| POST | `/v1/runs/{run_id}/stop` | [`stop_run`](../services/control-plane/app/routes/runs.py#L362) |
| POST | `/v1/runs/{run_id}/open` | [`open_app`](../services/control-plane/app/routes/runs.py#L381) |

### scim

Directory provisioning service.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/scim/v2/ServiceProviderConfig` | [`provider_config`](../services/control-plane/app/routes/scim.py#L136) |
| GET | `/scim/v2/ResourceTypes` | [`resource_types`](../services/control-plane/app/routes/scim.py#L146) |
| GET | `/scim/v2/Schemas` | [`schemas`](../services/control-plane/app/routes/scim.py#L153) |
| GET | `/scim/v2/Schemas/{schema_id}` | [`schema`](../services/control-plane/app/routes/scim.py#L164) |
| GET | `/scim/v2/ResourceTypes/{type_id}` | [`resource_type`](../services/control-plane/app/routes/scim.py#L172) |
| GET | `/scim/v2/{collection}` | [`list_resources`](../services/control-plane/app/routes/scim.py#L180) |
| GET | `/scim/v2/{collection}/{pid}` | [`get_resource`](../services/control-plane/app/routes/scim.py#L199) |
| POST | `/scim/v2/{collection}` | [`create_resource`](../services/control-plane/app/routes/scim.py#L206) |
| PATCH | `/scim/v2/{collection}/{pid}` | [`update_resource`](../services/control-plane/app/routes/scim.py#L239) |
| PUT | `/scim/v2/{collection}/{pid}` | [`update_resource`](../services/control-plane/app/routes/scim.py#L239) |
| DELETE | `/scim/v2/{collection}/{pid}` | [`delete_resource`](../services/control-plane/app/routes/scim.py#L300) |

### sql

SQL admission/results; engine policy callbacks.

| Method | Path | Handler / source |
|---|---|---|
| POST | `/v1/statement` | [`statement`](../services/control-plane/app/routes/sql.py#L143) |
| GET, DELETE | `/v1/sql/pages/{token}` | [`page`](../services/control-plane/app/routes/sql.py#L191) |
| GET | `/v1/sql/queries/{query_id}` | [`query_status`](../services/control-plane/app/routes/sql.py#L210) |
| GET | `/v1/info` | [`info`](../services/control-plane/app/routes/sql.py#L220) |
| POST | `/v1/sql/query` | [`query_request`](../services/control-plane/app/routes/sql.py#L251) |
| GET | `/internal/trino/jwks` | [`trino_jwks`](../services/control-plane/app/routes/sql.py#L331) |
| POST | `/internal/trino/allow` | [`allow`](../services/control-plane/app/routes/sql.py#L337) |
| POST | `/internal/trino/layers/{layer}/allow` | [`layer_allow`](../services/control-plane/app/routes/sql.py#L342) |
| POST | `/internal/trino/batch` | [`batch`](../services/control-plane/app/routes/sql.py#L375) |
| POST | `/internal/trino/layers/{layer}/batch` | [`layer_batch`](../services/control-plane/app/routes/sql.py#L380) |
| POST | `/internal/trino/row-filters` | [`row_filters`](../services/control-plane/app/routes/sql.py#L402) |
| POST | `/internal/trino/layers/{layer}/row-filters` | [`layer_row_filters`](../services/control-plane/app/routes/sql.py#L407) |
| POST | `/internal/trino/batch-column-masks` | [`column_masks`](../services/control-plane/app/routes/sql.py#L420) |
| POST | `/internal/trino/layers/{layer}/batch-column-masks` | [`layer_column_masks`](../services/control-plane/app/routes/sql.py#L425) |

### webhooks

Webhooks; delivery; rules; channels.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/webhooks` | [`list_webhooks`](../services/control-plane/app/routes/webhooks.py#L56) |
| PUT | `/v1/admin/webhooks/{name}` | [`put_webhook`](../services/control-plane/app/routes/webhooks.py#L63) |
| DELETE | `/v1/admin/webhooks/{name}` | [`delete_webhook`](../services/control-plane/app/routes/webhooks.py#L93) |
| POST | `/v1/admin/webhooks/{name}/test` | [`test_webhook`](../services/control-plane/app/routes/webhooks.py#L106) |
| GET | `/v1/admin/webhooks/{name}/deliveries` | [`deliveries`](../services/control-plane/app/routes/webhooks.py#L121) |
| GET | `/v1/alerts/events` | [`alert_events`](../services/control-plane/app/routes/webhooks.py#L172) |
| GET | `/v1/alerts/channels` | [`alert_channels`](../services/control-plane/app/routes/webhooks.py#L177) |
| GET | `/v1/alerts/rules` | [`list_rules`](../services/control-plane/app/routes/webhooks.py#L183) |
| POST | `/v1/alerts/rules` | [`create_rule`](../services/control-plane/app/routes/webhooks.py#L196) |
| PUT | `/v1/alerts/rules/{rule_id}` | [`update_rule`](../services/control-plane/app/routes/webhooks.py#L207) |
| DELETE | `/v1/alerts/rules/{rule_id}` | [`delete_rule`](../services/control-plane/app/routes/webhooks.py#L220) |

### workspace_templates

Workspace templates.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/workspace-templates` | [`list_templates`](../services/control-plane/app/routes/workspace_templates.py#L62) |
| POST | `/v1/admin/workspace-templates` | [`create_template`](../services/control-plane/app/routes/workspace_templates.py#L67) |
| GET | `/v1/admin/workspace-templates/{template_id}` | [`get_template`](../services/control-plane/app/routes/workspace_templates.py#L80) |
| PUT | `/v1/admin/workspace-templates/{template_id}` | [`update_template`](../services/control-plane/app/routes/workspace_templates.py#L88) |
| DELETE | `/v1/admin/workspace-templates/{template_id}` | [`delete_template`](../services/control-plane/app/routes/workspace_templates.py#L103) |

### workspaces

Own context; workspace lifecycle; pools; token renewal.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/me` | [`me`](../services/control-plane/app/routes/workspaces.py#L25) |
| PUT | `/v1/me/active-layer` | [`select_active_layer`](../services/control-plane/app/routes/workspaces.py#L101) |
| PUT | `/v1/me/active-group` | [`select_active_group`](../services/control-plane/app/routes/workspaces.py#L121) |
| GET | `/v1/workspaces` | [`list_workspaces`](../services/control-plane/app/routes/workspaces.py#L150) |
| POST | `/v1/workspaces` | [`create_workspace`](../services/control-plane/app/routes/workspaces.py#L161) |
| POST | `/v1/workspaces/{ws_id}/start` | [`start_workspace`](../services/control-plane/app/routes/workspaces.py#L183) |
| POST | `/v1/workspaces/{ws_id}/stop` | [`stop_workspace`](../services/control-plane/app/routes/workspaces.py#L266) |
| DELETE | `/v1/workspaces/{ws_id}` | [`delete_workspace`](../services/control-plane/app/routes/workspaces.py#L283) |
| GET | `/v1/pools` | [`list_pools`](../services/control-plane/app/routes/workspaces.py#L301) |
| GET | `/v1/workspaces/{ws_id}/config` | [`workspace_config`](../services/control-plane/app/routes/workspaces.py#L314) |
| POST | `/v1/workspaces/{ws_id}/open` | [`open_workspace`](../services/control-plane/app/routes/workspaces.py#L326) |
| POST | `/v1/executions/token` | [`refresh_run_token`](../services/control-plane/app/routes/workspaces.py#L336) |

### main

Public bootstrap, schema and health endpoints.

| Method | Path | Handler / source |
|---|---|---|
| GET | `/healthz` | [`healthz`](../services/control-plane/app/main.py#L192) |
| GET | `/readyz` | [`readyz`](../services/control-plane/app/main.py#L197) |
| GET | `/v1/schema/fdp.json` | [`fdp_schema`](../services/control-plane/app/main.py#L214) |
| GET | `/v1/schema/layer-infrastructure.json` | [`layer_infrastructure_schema`](../services/control-plane/app/main.py#L220) |
| GET | `/v1/client-config` | [`client_config`](../services/control-plane/app/main.py#L226) |

## Desktop pages and aliases

Every tab kind below requires an explicit page gate, including aliases and detail pages. Object-specific views additionally require target permissions. Aliases must not bypass the canonical gate.

| Tab kind | Rendering source |
|---|---|
| `kafka` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L91) |
| `kafka-clusters` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L92) |
| `apis` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L73) |
| `api` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L74) |
| `catalog` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L75) |
| `catalog-object` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L79) |
| `users` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L83) |
| `access` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L80) |
| `home` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L84) |
| `workspaces` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L85) |
| `runs` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L86) |
| `workspace` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L88) |
| `run` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L89) |
| `data` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L93) |
| `source` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L90) |
| `schema` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L94) |
| `data-object` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L95) |
| `pool` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L96) |
| `folder` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L97) |
| `service` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L98) |
| `services` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L99) |
| `deploy` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L100) |
| `workload` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L101) |
| `secrets` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L118) |
| `docs` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L119) |
| `git-credentials` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L124) |
| `packages` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L125) |
| `package` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L126) |
| `package-imports` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L127) |
| `products` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L76) |
| `product` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L77) |
| `access-requests` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L78) |
| `app-profiles` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L103) |
| `repos` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L116) |
| `deadlines` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L129) |
| `privacy-reviews` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L107) |
| `privacy-case` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L110) |
| `case` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L110) |
| `privacy-memberships` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L108) |
| `privacy-register` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L109) |
| `privacy-policies` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L106) |
| `privacy-forms` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L111) |
| `privacy-form` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L113) |
| `privacy-roles` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L112) |
| `privacy-classifications` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L105) |
| `admin-app-profiles` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L103) |
| `admin-notifications` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L104) |
| `admin-classifications` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L105) |
| `admin-layers` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L102) |
| `admin-sources` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L115) |
| `admin-repos` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L116) |
| `admin-runtimes` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L117) |
| `admin-service-accounts` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L99) |
| `admin-pools` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L120) |
| `admin-workspace-templates` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L121) |
| `admin-network` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L122) |
| `admin-query-log` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L123) |
| `admin-audit` | [TabContent](../apps/desktop/src/views/Tabs.tsx#L128) |

## Object tabs and non-page entry points

- Object sections: overview, permissions, settings, preview, runs, SQL, lineage, policies, versions, logs, alerts.
- Product sections: overview, options, lineage, used-by, permissions, alerts.
- Catalog sections: overview, lineage, governance, SLA, access, metrics.
- API sections: operations, settings, calls, permissions; endpoint groups and operations have their own target scope.
- Group sections: members, has-access, settings.
- Workspace editor, Git picker, file access, run launcher, background jobs, auto-update and template synchronization need the same object/action checks.
- SDKs, Python/R package protocols, VS Code extension, generated app/editor URLs, metadata SQL and controller reconciliation are independent enforcement entry points.
- Framework-generated `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc`: explicitly classify as public static API documentation or protect with a documentation Viewer gate; never embed tenant records or secrets.
- Public external documentation links have no application data access; generated workload documentation requires workload/artifact permission.

Source coverage: **318 declared HTTP route entries** and **58 desktop tab kinds**. Multi-method entries represent more than one HTTP method.
