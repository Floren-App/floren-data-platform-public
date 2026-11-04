# Route and page inventory

Generated from mounted FastAPI routes and desktop page mappings. Entry-point inventory is not proof of authorization coverage; consult the [matrix](permission-matrix.md) and [migration status](scoped-permissions.md).

## HTTP entry points

### admin

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/audit` | [`audit_log`](../services/control-plane/app/routes/admin.py#L523) |
| GET | `/v1/admin/datasources` | [`list_sources`](../services/control-plane/app/routes/admin.py#L40) |
| DELETE | `/v1/admin/datasources/{name}` | [`delete_source`](../services/control-plane/app/routes/admin.py#L184) |
| GET | `/v1/admin/datasources/{name}` | [`read_source`](../services/control-plane/app/routes/admin.py#L51) |
| PUT | `/v1/admin/datasources/{name}` | [`upsert_source`](../services/control-plane/app/routes/admin.py#L58) |
| POST | `/v1/admin/datasources/{name}/grants` | [`grant`](../services/control-plane/app/routes/admin.py#L220) |
| DELETE | `/v1/admin/datasources/{name}/grants/{group}` | [`revoke`](../services/control-plane/app/routes/admin.py#L225) |
| GET | `/v1/admin/pools` | [`list_pools`](../services/control-plane/app/routes/admin.py#L457) |
| DELETE | `/v1/admin/pools/{name}` | [`delete_pool`](../services/control-plane/app/routes/admin.py#L499) |
| PUT | `/v1/admin/pools/{name}` | [`upsert_pool`](../services/control-plane/app/routes/admin.py#L463) |
| DELETE | `/v1/admin/settings/default-warehouse` | [`clear_default_warehouse`](../services/control-plane/app/routes/admin.py#L341) |
| GET | `/v1/admin/settings/default-warehouse` | [`default_warehouse_setting`](../services/control-plane/app/routes/admin.py#L288) |
| PUT | `/v1/admin/settings/default-warehouse` | [`set_default_warehouse`](../services/control-plane/app/routes/admin.py#L306) |
| GET | `/v1/admin/settings/egress` | [`egress_allowlist`](../services/control-plane/app/routes/admin.py#L379) |
| PUT | `/v1/admin/settings/egress` | [`set_egress_allowlist`](../services/control-plane/app/routes/admin.py#L389) |
| DELETE | `/v1/admin/settings/workspaces` | [`reset_workspace_settings`](../services/control-plane/app/routes/admin.py#L263) |
| GET | `/v1/admin/settings/workspaces` | [`workspace_settings`](../services/control-plane/app/routes/admin.py#L237) |
| PUT | `/v1/admin/settings/workspaces` | [`set_workspace_settings`](../services/control-plane/app/routes/admin.py#L244) |
| GET | `/v1/admin/storage-types` | [`storage_type_list`](../services/control-plane/app/routes/admin.py#L34) |

### apis

| Method | Path | Handler / source |
|---|---|---|
| DELETE, GET, PATCH, POST, PUT | `/gw/{slug}` | [`inbound`](../services/control-plane/app/routes/apis.py#L531) |
| DELETE, GET, PATCH, POST, PUT | `/gw/{slug}/{path:path}` | [`inbound`](../services/control-plane/app/routes/apis.py#L531) |
| GET | `/v1/apis` | [`list_apis`](../services/control-plane/app/routes/apis.py#L119) |
| POST | `/v1/apis` | [`create_api`](../services/control-plane/app/routes/apis.py#L171) |
| DELETE | `/v1/apis/{api_id}` | [`delete_api`](../services/control-plane/app/routes/apis.py#L226) |
| GET | `/v1/apis/{api_id}` | [`get_api`](../services/control-plane/app/routes/apis.py#L199) |
| PUT | `/v1/apis/{api_id}` | [`update_api`](../services/control-plane/app/routes/apis.py#L210) |
| GET | `/v1/apis/{api_id}/calls` | [`list_calls`](../services/control-plane/app/routes/apis.py#L373) |
| POST | `/v1/apis/{api_id}/operations` | [`create_operation`](../services/control-plane/app/routes/apis.py#L306) |
| DELETE | `/v1/apis/{api_id}/operations/{op_id}` | [`delete_operation`](../services/control-plane/app/routes/apis.py#L357) |
| PUT | `/v1/apis/{api_id}/operations/{op_id}` | [`update_operation`](../services/control-plane/app/routes/apis.py#L333) |
| DELETE, GET, PATCH, POST, PUT | `/v1/egress/{slug}/{path:path}` | [`egress`](../services/control-plane/app/routes/apis.py#L543) |

### app_profiles

| Method | Path | Handler / source |
|---|---|---|
| DELETE | `/v1/admin/app-profiles/{name}` | [`delete_profile`](../services/control-plane/app/routes/app_profiles.py#L69) |
| PUT | `/v1/admin/app-profiles/{name}` | [`put_profile`](../services/control-plane/app/routes/app_profiles.py#L45) |
| GET | `/v1/app-profiles` | [`list_profiles`](../services/control-plane/app/routes/app_profiles.py#L38) |

### backups

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/backups` | [`history`](../services/control-plane/app/routes/backups.py#L79) |
| POST | `/v1/backups` | [`create`](../services/control-plane/app/routes/backups.py#L88) |
| DELETE | `/v1/backups/configuration` | [`disable`](../services/control-plane/app/routes/backups.py#L68) |
| GET | `/v1/backups/configuration` | [`read`](../services/control-plane/app/routes/backups.py#L45) |
| PUT | `/v1/backups/configuration` | [`configure`](../services/control-plane/app/routes/backups.py#L51) |
| GET | `/v1/backups/destinations` | [`destinations`](../services/control-plane/app/routes/backups.py#L156) |
| POST | `/v1/backups/destinations` | [`create_destination`](../services/control-plane/app/routes/backups.py#L163) |
| DELETE | `/v1/backups/destinations/{ident}` | [`delete_destination`](../services/control-plane/app/routes/backups.py#L198) |
| GET | `/v1/backups/destinations/{ident}` | [`destination_detail`](../services/control-plane/app/routes/backups.py#L178) |
| PUT | `/v1/backups/destinations/{ident}` | [`update_destination`](../services/control-plane/app/routes/backups.py#L184) |
| GET | `/v1/backups/plans` | [`list_plans`](../services/control-plane/app/routes/backups.py#L208) |
| POST | `/v1/backups/plans` | [`create_plan`](../services/control-plane/app/routes/backups.py#L215) |
| DELETE | `/v1/backups/plans/{ident}` | [`delete_plan`](../services/control-plane/app/routes/backups.py#L232) |
| GET | `/v1/backups/plans/{ident}` | [`plan_detail`](../services/control-plane/app/routes/backups.py#L221) |
| PUT | `/v1/backups/plans/{ident}` | [`update_plan`](../services/control-plane/app/routes/backups.py#L227) |
| GET | `/v1/backups/plans/{ident}/runs` | [`plan_history`](../services/control-plane/app/routes/backups.py#L246) |
| POST | `/v1/backups/plans/{ident}/runs` | [`run_plan`](../services/control-plane/app/routes/backups.py#L239) |
| DELETE | `/v1/backups/plans/{ident}/runs/{run_id}` | [`delete_archive`](../services/control-plane/app/routes/backups.py#L253) |
| GET | `/v1/backups/recovery` | [`recovery`](../services/control-plane/app/routes/backups.py#L97) |
| POST | `/v1/backups/recovery/resume` | [`resume`](../services/control-plane/app/routes/backups.py#L114) |
| GET | `/v1/backups/sources` | [`sources`](../services/control-plane/app/routes/backups.py#L144) |

### catalog

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/catalog/assets/{ident}` | [`detail`](../services/control-plane/app/routes/catalog.py#L222) |
| PUT | `/v1/catalog/assets/{ident}/columns` | [`put_columns`](../services/control-plane/app/routes/catalog.py#L444) |
| PUT | `/v1/catalog/assets/{ident}/governance` | [`governance`](../services/control-plane/app/routes/catalog.py#L233) |
| GET | `/v1/catalog/assets/{ident}/lineage` | [`lineage`](../services/control-plane/app/routes/catalog.py#L299) |
| PUT | `/v1/catalog/assets/{ident}/sla` | [`sla`](../services/control-plane/app/routes/catalog.py#L274) |
| GET | `/v1/catalog/classifications` | [`list_classifications`](../services/control-plane/app/routes/catalog.py#L408) |
| PUT | `/v1/catalog/classifications` | [`put_classifications`](../services/control-plane/app/routes/catalog.py#L427) |
| POST | `/v1/catalog/ingest/airflow` | [`ingest_airflow`](../services/control-plane/app/routes/catalog.py#L366) |
| POST | `/v1/catalog/ingest/dbt` | [`ingest_dbt`](../services/control-plane/app/routes/catalog.py#L355) |
| POST | `/v1/catalog/ingest/program` | [`ingest_program`](../services/control-plane/app/routes/catalog.py#L373) |
| GET | `/v1/catalog/resources/{resource_id}/asset` | [`resource_asset`](../services/control-plane/app/routes/catalog.py#L205) |
| POST | `/v1/catalog/resources/{resource_id}/asset` | [`ensure_asset`](../services/control-plane/app/routes/catalog.py#L476) |
| POST | `/v1/catalog/resources/{resource_id}/import/airflow` | [`import_airflow`](../services/control-plane/app/routes/catalog.py#L334) |
| POST | `/v1/catalog/resources/{resource_id}/import/dbt` | [`import_dbt`](../services/control-plane/app/routes/catalog.py#L323) |
| GET | `/v1/catalog/search` | [`search`](../services/control-plane/app/routes/catalog.py#L144) |
| GET | `/v1/catalog/suggest` | [`suggest`](../services/control-plane/app/routes/catalog.py#L196) |

### compliance

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/compliance/cases` | [`list_cases`](../services/control-plane/app/routes/compliance.py#L415) |
| POST | `/v1/compliance/cases` | [`create_case`](../services/control-plane/app/routes/compliance.py#L395) |
| GET | `/v1/compliance/cases/{case_id}` | [`get_case`](../services/control-plane/app/routes/compliance.py#L436) |
| PATCH | `/v1/compliance/cases/{case_id}` | [`update_case`](../services/control-plane/app/routes/compliance.py#L449) |
| POST | `/v1/compliance/cases/{case_id}/comments` | [`comment`](../services/control-plane/app/routes/compliance.py#L510) |
| POST | `/v1/compliance/cases/{case_id}/decision` | [`decide_case`](../services/control-plane/app/routes/compliance.py#L492) |
| POST | `/v1/compliance/cases/{case_id}/files` | [`add_file`](../services/control-plane/app/routes/compliance.py#L540) |
| GET | `/v1/compliance/cases/{case_id}/files/{file_id}` | [`get_file`](../services/control-plane/app/routes/compliance.py#L565) |
| POST | `/v1/compliance/cases/{case_id}/renew` | [`renew_case`](../services/control-plane/app/routes/compliance.py#L708) |
| POST | `/v1/compliance/cases/{case_id}/submit` | [`submit_case`](../services/control-plane/app/routes/compliance.py#L466) |
| POST | `/v1/compliance/cases/{case_id}/withdraw` | [`withdraw_case`](../services/control-plane/app/routes/compliance.py#L521) |
| GET | `/v1/compliance/deadlines` | [`list_deadlines`](../services/control-plane/app/routes/compliance.py#L726) |
| GET | `/v1/compliance/forms` | [`list_forms`](../services/control-plane/app/routes/compliance.py#L51) |
| DELETE | `/v1/compliance/forms/{form_id}` | [`delete_form`](../services/control-plane/app/routes/compliance.py#L103) |
| GET | `/v1/compliance/forms/{form_id}` | [`get_form`](../services/control-plane/app/routes/compliance.py#L57) |
| POST | `/v1/compliance/forms/{form_id}/publish` | [`publish_form`](../services/control-plane/app/routes/compliance.py#L90) |
| PUT | `/v1/compliance/forms/{name}` | [`put_form`](../services/control-plane/app/routes/compliance.py#L66) |
| GET | `/v1/compliance/memberships` | [`pending_memberships`](../services/control-plane/app/routes/compliance.py#L578) |
| POST | `/v1/compliance/memberships/{group_id}/{principal_id}` | [`decide_membership`](../services/control-plane/app/routes/compliance.py#L604) |
| GET | `/v1/compliance/policies` | [`list_policies`](../services/control-plane/app/routes/compliance.py#L147) |
| DELETE | `/v1/compliance/policies/{layer_id}/{level}` | [`delete_policy`](../services/control-plane/app/routes/compliance.py#L189) |
| PUT | `/v1/compliance/policies/{layer_id}/{level}` | [`put_policy`](../services/control-plane/app/routes/compliance.py#L159) |
| GET | `/v1/compliance/register` | [`access_register`](../services/control-plane/app/routes/compliance.py#L661) |
| POST | `/v1/compliance/requirement` | [`requirement`](../services/control-plane/app/routes/compliance.py#L293) |
| GET | `/v1/compliance/roles` | [`list_roles`](../services/control-plane/app/routes/compliance.py#L222) |
| POST | `/v1/compliance/roles` | [`add_role`](../services/control-plane/app/routes/compliance.py#L229) |
| DELETE | `/v1/compliance/roles/{role_id}` | [`remove_role`](../services/control-plane/app/routes/compliance.py#L249) |

### compute

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/compute/warehouses` | [`warehouses`](../services/control-plane/app/routes/compute.py#L102) |

### connect

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/connect` | [`available`](../services/control-plane/app/routes/connect.py#L22) |
| GET | `/v1/connect/{name}` | [`connect`](../services/control-plane/app/routes/connect.py#L28) |

### data_browser

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/data/schemas/{resource_id}/children` | [`children`](../services/control-plane/app/routes/data_browser.py#L160) |
| GET | `/v1/data/schemas/{resource_id}/file` | [`file_preview`](../services/control-plane/app/routes/data_browser.py#L235) |
| GET | `/v1/data/schemas/{resource_id}/preview` | [`preview`](../services/control-plane/app/routes/data_browser.py#L18) |

### data_objects

| Method | Path | Handler / source |
|---|---|---|
| POST | `/v1/data-objects` | [`register`](../services/control-plane/app/routes/data_objects.py#L42) |
| DELETE | `/v1/data-objects/{ident}` | [`unregister`](../services/control-plane/app/routes/data_objects.py#L91) |
| GET | `/v1/data-objects/{ident}` | [`read`](../services/control-plane/app/routes/data_objects.py#L83) |
| POST | `/v1/data-objects/{ident}/session` | [`file_session`](../services/control-plane/app/routes/data_objects.py#L107) |

### data_policies

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/data-policies` | [`list_policies`](../services/control-plane/app/routes/data_policies.py#L101) |
| POST | `/v1/data-policies` | [`create_policy`](../services/control-plane/app/routes/data_policies.py#L136) |
| POST | `/v1/data-policies/normalize` | [`normalize`](../services/control-plane/app/routes/data_policies.py#L121) |
| DELETE | `/v1/data-policies/{policy_id}` | [`delete_policy`](../services/control-plane/app/routes/data_policies.py#L179) |
| GET | `/v1/data-policies/{policy_id}` | [`get_policy`](../services/control-plane/app/routes/data_policies.py#L128) |
| PUT | `/v1/data-policies/{policy_id}` | [`update_policy`](../services/control-plane/app/routes/data_policies.py#L160) |

### delegation

| Method | Path | Handler / source |
|---|---|---|
| POST | `/v1/delegated/files` | [`files`](../services/control-plane/app/routes/delegation.py#L97) |
| POST | `/v1/delegated/query` | [`query`](../services/control-plane/app/routes/delegation.py#L26) |

### deploy

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/repos` | [`admin_repos`](../services/control-plane/app/routes/deploy.py#L58) |
| DELETE | `/v1/admin/repos/{name}` | [`delete_repo`](../services/control-plane/app/routes/deploy.py#L101) |
| PUT | `/v1/admin/repos/{name}` | [`upsert_repo`](../services/control-plane/app/routes/deploy.py#L64) |
| POST | `/v1/orchestrate/runs` | [`orchestrate`](../services/control-plane/app/routes/deploy.py#L546) |
| GET | `/v1/repos` | [`list_repos`](../services/control-plane/app/routes/deploy.py#L117) |
| GET | `/v1/repos/{repo_id}/branches` | [`repo_branches`](../services/control-plane/app/routes/deploy.py#L125) |
| POST | `/v1/repos/{repo_id}/pull` | [`repo_pull`](../services/control-plane/app/routes/deploy.py#L139) |
| GET | `/v1/workloads` | [`catalog`](../services/control-plane/app/routes/deploy.py#L228) |
| POST | `/v1/workloads` | [`create_workload`](../services/control-plane/app/routes/deploy.py#L236) |
| DELETE | `/v1/workloads/{pub_id}` | [`undeploy`](../services/control-plane/app/routes/deploy.py#L464) |
| GET | `/v1/workloads/{pub_id}` | [`get_workload`](../services/control-plane/app/routes/deploy.py#L330) |
| PATCH | `/v1/workloads/{pub_id}` | [`update_workload`](../services/control-plane/app/routes/deploy.py#L383) |
| POST | `/v1/workloads/{pub_id}/open` | [`open_workload`](../services/control-plane/app/routes/deploy.py#L481) |
| GET | `/v1/workloads/{pub_id}/orchestrator-runs` | [`orchestrator_runs`](../services/control-plane/app/routes/deploy.py#L491) |
| POST | `/v1/workloads/{pub_id}/runs` | [`run_deployed`](../services/control-plane/app/routes/deploy.py#L508) |
| GET | `/v1/workloads/{pub_id}/source` | [`source`](../services/control-plane/app/routes/deploy.py#L603) |
| POST | `/v1/workloads/{pub_id}/sync` | [`sync_now`](../services/control-plane/app/routes/deploy.py#L450) |
| GET | `/v1/workloads/{pub_id}/versions` | [`workload_versions`](../services/control-plane/app/routes/deploy.py#L335) |

### gateway

| Method | Path | Handler / source |
|---|---|---|
| GET | `/internal/auth/forward` | [`forward_auth`](../services/control-plane/app/routes/gateway.py#L254) |
| GET | `/internal/egress/allowlist` | [`egress_allowlist`](../services/control-plane/app/routes/gateway.py#L78) |
| POST | `/internal/egress/authorize` | [`egress_authorize`](../services/control-plane/app/routes/gateway.py#L93) |
| GET | `/internal/metrics` | [`platform_metrics`](../services/control-plane/app/routes/gateway.py#L50) |
| GET | `/internal/metrics/certificates` | [`certificate_metrics`](../services/control-plane/app/routes/gateway.py#L65) |
| GET | `/internal/traefik/config` | [`traefik_config`](../services/control-plane/app/routes/gateway.py#L126) |
| GET | `/p/{slug}/` | [`dbt_docs`](../services/control-plane/app/routes/gateway.py#L373) |
| GET | `/p/{slug}/index.html` | [`dbt_docs`](../services/control-plane/app/routes/gateway.py#L373) |
| GET | `/ws/{ws_id}/{path:path}` | [`workspace_waiting`](../services/control-plane/app/routes/gateway.py#L347) |

### git_credentials

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/me/git-credentials` | [`list_git_credentials`](../services/control-plane/app/routes/git_credentials.py#L77) |
| POST | `/v1/me/git-credentials` | [`create_git_credential`](../services/control-plane/app/routes/git_credentials.py#L85) |
| DELETE | `/v1/me/git-credentials/{credential_id}` | [`delete_git_credential`](../services/control-plane/app/routes/git_credentials.py#L111) |
| PUT | `/v1/me/git-credentials/{credential_id}` | [`update_git_credential`](../services/control-plane/app/routes/git_credentials.py#L100) |
| PUT | `/v1/workspaces/{ws_id}/git` | [`choose_workspace_git`](../services/control-plane/app/routes/git_credentials.py#L129) |
| GET | `/v1/workspaces/{ws_id}/git-credential` | [`workspace_git_credential`](../services/control-plane/app/routes/git_credentials.py#L149) |

### identity

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/runtimes` | [`admin_runtimes`](../services/control-plane/app/routes/identity.py#L62) |
| DELETE | `/v1/admin/runtimes/{kind}/{version}` | [`delete_runtime`](../services/control-plane/app/routes/identity.py#L101) |
| PUT | `/v1/admin/runtimes/{kind}/{version}` | [`upsert_runtime`](../services/control-plane/app/routes/identity.py#L67) |
| DELETE | `/v1/admin/service-accounts/{name}` | [`delete_service_account`](../services/control-plane/app/routes/identity.py#L368) |
| PUT | `/v1/admin/service-accounts/{name}` | [`upsert_service_account`](../services/control-plane/app/routes/identity.py#L307) |
| GET | `/v1/runtimes` | [`runtimes`](../services/control-plane/app/routes/identity.py#L56) |
| GET | `/v1/secrets` | [`list_secrets`](../services/control-plane/app/routes/identity.py#L161) |
| DELETE | `/v1/secrets/{scope}/{owner}/{name}` | [`delete_secret`](../services/control-plane/app/routes/identity.py#L204) |
| PUT | `/v1/secrets/{scope}/{owner}/{name}` | [`set_secret`](../services/control-plane/app/routes/identity.py#L173) |
| GET | `/v1/service-accounts` | [`list_service_accounts`](../services/control-plane/app/routes/identity.py#L298) |
| GET | `/v1/service-accounts/{name}/tokens` | [`list_tokens`](../services/control-plane/app/routes/identity.py#L386) |
| POST | `/v1/service-accounts/{name}/tokens` | [`create_token`](../services/control-plane/app/routes/identity.py#L394) |
| DELETE | `/v1/service-accounts/{name}/tokens/{token_id}` | [`revoke_token`](../services/control-plane/app/routes/identity.py#L413) |

### kafka

| Method | Path | Handler / source |
|---|---|---|
| GET | `/internal/kafka/jwks` | [`jwks`](../services/control-plane/app/routes/kafka.py#L237) |
| POST | `/internal/kafka/layers/{layer_id}/allow` | [`allow`](../services/control-plane/app/routes/kafka.py#L243) |
| GET | `/v1/kafka/clusters` | [`clusters`](../services/control-plane/app/routes/kafka.py#L61) |
| DELETE | `/v1/kafka/clusters/{ident}` | [`unregister_cluster`](../services/control-plane/app/routes/kafka.py#L94) |
| GET | `/v1/kafka/clusters/{ident}` | [`get_cluster`](../services/control-plane/app/routes/kafka.py#L74) |
| PUT | `/v1/kafka/clusters/{ident}/running` | [`set_running`](../services/control-plane/app/routes/kafka.py#L268) |
| POST | `/v1/kafka/clusters/{layer_id}` | [`register_cluster`](../services/control-plane/app/routes/kafka.py#L39) |
| GET | `/v1/kafka/resolve` | [`resolve_topic`](../services/control-plane/app/routes/kafka.py#L103) |
| GET | `/v1/kafka/topics` | [`topics`](../services/control-plane/app/routes/kafka.py#L67) |
| POST | `/v1/kafka/topics` | [`create_topic`](../services/control-plane/app/routes/kafka.py#L121) |
| DELETE | `/v1/kafka/topics/{ident}` | [`delete_topic`](../services/control-plane/app/routes/kafka.py#L178) |
| GET | `/v1/kafka/topics/{ident}` | [`get_topic`](../services/control-plane/app/routes/kafka.py#L114) |
| PUT | `/v1/kafka/topics/{ident}` | [`update_topic`](../services/control-plane/app/routes/kafka.py#L159) |
| GET | `/v1/kafka/topics/{ident}/deletion` | [`deletion`](../services/control-plane/app/routes/kafka.py#L188) |
| POST | `/v1/kafka/topics/{ident}/observations` | [`observation`](../services/control-plane/app/routes/kafka.py#L206) |
| POST | `/v1/kafka/topics/{ident}/session` | [`session`](../services/control-plane/app/routes/kafka.py#L197) |

### layers

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/certificates` | [`certificates`](../services/control-plane/app/routes/layers.py#L27) |
| POST | `/v1/admin/layers` | [`create`](../services/control-plane/app/routes/layers.py#L182) |
| DELETE | `/v1/admin/layers/{ident}` | [`remove`](../services/control-plane/app/routes/layers.py#L229) |
| PUT | `/v1/admin/layers/{ident}` | [`update`](../services/control-plane/app/routes/layers.py#L208) |
| GET | `/v1/admin/layers/{ident}/infrastructure` | [`infrastructure_state`](../services/control-plane/app/routes/layers.py#L19) |
| PUT | `/v1/admin/layers/{ident}/infrastructure` | [`configure_infrastructure`](../services/control-plane/app/routes/layers.py#L36) |
| POST | `/v1/admin/layers/{ident}/infrastructure/retire` | [`retire_infrastructure`](../services/control-plane/app/routes/layers.py#L89) |
| POST | `/v1/admin/layers/{ident}/infrastructure/retry` | [`retry_infrastructure`](../services/control-plane/app/routes/layers.py#L70) |
| GET | `/v1/admin/layers/{ident}/operations` | [`infrastructure_operations`](../services/control-plane/app/routes/layers.py#L110) |
| DELETE | `/v1/admin/settings/default-layer` | [`clear_default_layer`](../services/control-plane/app/routes/layers.py#L171) |
| GET | `/v1/admin/settings/default-layer` | [`default_layer`](../services/control-plane/app/routes/layers.py#L147) |
| PUT | `/v1/admin/settings/default-layer` | [`set_default_layer`](../services/control-plane/app/routes/layers.py#L156) |
| GET | `/v1/layers` | [`listing`](../services/control-plane/app/routes/layers.py#L139) |

### logs

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/logs` | [`read_logs`](../services/control-plane/app/routes/logs.py#L149) |
| POST | `/v1/logs` | [`write_logs`](../services/control-plane/app/routes/logs.py#L77) |

### main

| Method | Path | Handler / source |
|---|---|---|
| GET | `/healthz` | [`healthz`](../services/control-plane/app/main.py#L202) |
| GET | `/readyz` | [`readyz`](../services/control-plane/app/main.py#L207) |
| GET | `/v1/client-config` | [`client_config`](../services/control-plane/app/main.py#L236) |
| GET | `/v1/schema/fdp.json` | [`fdp_schema`](../services/control-plane/app/main.py#L224) |
| GET | `/v1/schema/layer-infrastructure.json` | [`layer_infrastructure_schema`](../services/control-plane/app/main.py#L230) |

### measurements

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/catalog/alerts` | [`alerts`](../services/control-plane/app/routes/measurements.py#L248) |
| POST | `/v1/catalog/alerts/{incident_id}/acknowledge` | [`acknowledge`](../services/control-plane/app/routes/measurements.py#L268) |
| POST | `/v1/catalog/assets/{ident}/evaluate-sla` | [`evaluate_sla`](../services/control-plane/app/routes/measurements.py#L238) |
| GET | `/v1/catalog/assets/{ident}/metrics` | [`metrics`](../services/control-plane/app/routes/measurements.py#L88) |
| POST | `/v1/catalog/assets/{ident}/metrics` | [`define`](../services/control-plane/app/routes/measurements.py#L96) |
| GET | `/v1/catalog/assets/{ident}/metrics/{metric_id}/history` | [`history`](../services/control-plane/app/routes/measurements.py#L129) |
| POST | `/v1/catalog/assets/{ident}/metrics/{metric_id}/measure` | [`measure`](../services/control-plane/app/routes/measurements.py#L170) |
| POST | `/v1/catalog/assets/{ident}/metrics/{metric_id}/observations` | [`report`](../services/control-plane/app/routes/measurements.py#L153) |
| GET | `/v1/catalog/assets/{ident}/metrics/{metric_id}/versions` | [`versions`](../services/control-plane/app/routes/measurements.py#L139) |
| POST | `/v1/catalog/assets/{ident}/observations` | [`dataset_report`](../services/control-plane/app/routes/measurements.py#L213) |
| GET | `/v1/catalog/assets/{ident}/sla-history` | [`sla_history`](../services/control-plane/app/routes/measurements.py#L230) |

### packages

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/packages` | [`list_packages`](../services/control-plane/app/routes/packages.py#L139) |
| DELETE | `/v1/packages/allowlist` | [`reset_package_allowlist`](../services/control-plane/app/routes/packages.py#L197) |
| GET | `/v1/packages/allowlist` | [`package_allowlist`](../services/control-plane/app/routes/packages.py#L166) |
| PUT | `/v1/packages/allowlist` | [`set_package_allowlist`](../services/control-plane/app/routes/packages.py#L173) |
| GET | `/v1/packages/imports` | [`list_imports`](../services/control-plane/app/routes/packages.py#L234) |
| POST | `/v1/packages/imports` | [`request_import`](../services/control-plane/app/routes/packages.py#L215) |
| GET | `/v1/packages/imports/{job_id}` | [`get_import`](../services/control-plane/app/routes/packages.py#L240) |
| GET | `/v1/packages/python/files/{package_key}/{filename}` | [`python_file`](../services/control-plane/app/routes/packages.py#L367) |
| POST | `/v1/packages/python/legacy/` | [`twine_upload`](../services/control-plane/app/routes/packages.py#L377) |
| GET | `/v1/packages/python/simple/` | [`simple_index`](../services/control-plane/app/routes/packages.py#L342) |
| GET | `/v1/packages/python/simple/{name}/` | [`simple_project`](../services/control-plane/app/routes/packages.py#L349) |
| GET | `/v1/packages/r/src/contrib/Archive/{name}/{filename}` | [`r_archive_file`](../services/control-plane/app/routes/packages.py#L403) |
| GET | `/v1/packages/r/src/contrib/PACKAGES` | [`r_packages`](../services/control-plane/app/routes/packages.py#L393) |
| GET | `/v1/packages/r/src/contrib/PACKAGES.gz` | [`r_packages_gz`](../services/control-plane/app/routes/packages.py#L398) |
| GET | `/v1/packages/r/src/contrib/{filename}` | [`r_source_file`](../services/control-plane/app/routes/packages.py#L412) |
| GET | `/v1/packages/x/{key}/python/files/{package_key}/{filename}` | [`python_file`](../services/control-plane/app/routes/packages.py#L367) |
| GET | `/v1/packages/x/{key}/python/simple/` | [`simple_index`](../services/control-plane/app/routes/packages.py#L342) |
| GET | `/v1/packages/x/{key}/python/simple/{name}/` | [`simple_project`](../services/control-plane/app/routes/packages.py#L349) |
| GET | `/v1/packages/x/{key}/r/src/contrib/Archive/{name}/{filename}` | [`r_archive_file`](../services/control-plane/app/routes/packages.py#L403) |
| GET | `/v1/packages/x/{key}/r/src/contrib/PACKAGES` | [`r_packages`](../services/control-plane/app/routes/packages.py#L393) |
| GET | `/v1/packages/x/{key}/r/src/contrib/PACKAGES.gz` | [`r_packages_gz`](../services/control-plane/app/routes/packages.py#L398) |
| GET | `/v1/packages/x/{key}/r/src/contrib/{filename}` | [`r_source_file`](../services/control-plane/app/routes/packages.py#L412) |
| POST | `/v1/packages/{kind}/upload` | [`upload`](../services/control-plane/app/routes/packages.py#L263) |
| GET | `/v1/packages/{kind}/{name}` | [`get_package`](../services/control-plane/app/routes/packages.py#L271) |
| DELETE | `/v1/packages/{kind}/{name}/versions/{version}` | [`delete_version`](../services/control-plane/app/routes/packages.py#L313) |
| DELETE | `/v1/packages/{kind}/{name}/versions/{version}/yank` | [`unyank`](../services/control-plane/app/routes/packages.py#L300) |
| POST | `/v1/packages/{kind}/{name}/versions/{version}/yank` | [`yank`](../services/control-plane/app/routes/packages.py#L286) |

### permissions

| Method | Path | Handler / source |
|---|---|---|
| POST | `/v1/permissions/assignments` | [`create`](../services/control-plane/app/routes/permissions.py#L99) |
| DELETE | `/v1/permissions/assignments/{ident}` | [`remove`](../services/control-plane/app/routes/permissions.py#L136) |
| GET | `/v1/permissions/assignments/{ident}` | [`read`](../services/control-plane/app/routes/permissions.py#L119) |
| PUT | `/v1/permissions/assignments/{ident}` | [`update`](../services/control-plane/app/routes/permissions.py#L124) |
| GET | `/v1/permissions/capabilities` | [`capabilities`](../services/control-plane/app/routes/permissions.py#L94) |
| GET | `/v1/permissions/catalog` | [`catalog`](../services/control-plane/app/routes/permissions.py#L86) |
| GET | `/v1/permissions/registry` | [`registry`](../services/control-plane/app/routes/permissions.py#L81) |
| POST | `/v1/permissions/roles` | [`create_role`](../services/control-plane/app/routes/permissions.py#L158) |
| DELETE | `/v1/permissions/roles/{ident}` | [`remove_role`](../services/control-plane/app/routes/permissions.py#L204) |
| GET | `/v1/permissions/roles/{ident}` | [`read_role`](../services/control-plane/app/routes/permissions.py#L171) |
| PUT | `/v1/permissions/roles/{ident}` | [`update_role`](../services/control-plane/app/routes/permissions.py#L180) |

### products

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/catalog/assets/{ident}/product` | [`asset_product`](../services/control-plane/app/routes/products.py#L334) |
| GET | `/v1/catalog/products` | [`list_products`](../services/control-plane/app/routes/products.py#L278) |
| POST | `/v1/catalog/products` | [`publish`](../services/control-plane/app/routes/products.py#L450) |
| GET | `/v1/catalog/products/candidates` | [`publish_candidates`](../services/control-plane/app/routes/products.py#L298) |
| DELETE | `/v1/catalog/products/{product_id}` | [`unpublish`](../services/control-plane/app/routes/products.py#L538) |
| GET | `/v1/catalog/products/{product_id}` | [`product_detail`](../services/control-plane/app/routes/products.py#L562) |
| PATCH | `/v1/catalog/products/{product_id}` | [`update_product`](../services/control-plane/app/routes/products.py#L500) |
| DELETE | `/v1/catalog/products/{product_id}/consumers/{group_id}` | [`delete_consumer`](../services/control-plane/app/routes/products.py#L838) |
| PUT | `/v1/catalog/products/{product_id}/consumers/{group_id}` | [`put_consumer`](../services/control-plane/app/routes/products.py#L807) |
| POST | `/v1/catalog/products/{product_id}/requests` | [`request_access`](../services/control-plane/app/routes/products.py#L680) |
| GET | `/v1/catalog/requests` | [`list_requests`](../services/control-plane/app/routes/products.py#L726) |
| DELETE | `/v1/catalog/requests/{request_id}` | [`withdraw`](../services/control-plane/app/routes/products.py#L793) |
| POST | `/v1/catalog/requests/{request_id}/decision` | [`decide`](../services/control-plane/app/routes/products.py#L744) |

### query_log

| Method | Path | Handler / source |
|---|---|---|
| POST | `/internal/trino/layers/{layer_id}/events` | [`trino_events`](../services/control-plane/app/routes/query_log.py#L32) |
| DELETE | `/v1/admin/settings/query-log` | [`reset_query_log_settings`](../services/control-plane/app/routes/query_log.py#L113) |
| GET | `/v1/admin/settings/query-log` | [`query_log_settings`](../services/control-plane/app/routes/query_log.py#L100) |
| PUT | `/v1/admin/settings/query-log` | [`set_query_log_settings`](../services/control-plane/app/routes/query_log.py#L105) |
| GET | `/v1/admin/siem` | [`siem_status`](../services/control-plane/app/routes/query_log.py#L24) |
| GET | `/v1/query-log` | [`list_queries`](../services/control-plane/app/routes/query_log.py#L54) |

### rbac

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/rbac/directory` | [`directory`](../services/control-plane/app/routes/rbac.py#L133) |
| POST | `/v1/rbac/folders` | [`create_folder`](../services/control-plane/app/routes/rbac.py#L451) |
| DELETE | `/v1/rbac/folders/{resource_id}` | [`delete_folder`](../services/control-plane/app/routes/rbac.py#L531) |
| GET | `/v1/rbac/groups` | [`groups`](../services/control-plane/app/routes/rbac.py#L170) |
| POST | `/v1/rbac/groups` | [`create_group`](../services/control-plane/app/routes/rbac.py#L299) |
| DELETE | `/v1/rbac/groups/{group_id}` | [`delete_group`](../services/control-plane/app/routes/rbac.py#L348) |
| PUT | `/v1/rbac/groups/{group_id}` | [`update_group`](../services/control-plane/app/routes/rbac.py#L319) |
| GET | `/v1/rbac/groups/{group_id}/access` | [`group_access`](../services/control-plane/app/routes/rbac.py#L243) |
| GET | `/v1/rbac/groups/{group_id}/assignments` | [`group_assignments`](../services/control-plane/app/routes/rbac.py#L186) |
| POST | `/v1/rbac/groups/{group_id}/bindings` | [`bind`](../services/control-plane/app/routes/rbac.py#L388) |
| DELETE | `/v1/rbac/groups/{group_id}/bindings/{principal_id}` | [`unbind`](../services/control-plane/app/routes/rbac.py#L407) |
| GET | `/v1/rbac/principals/{principal_id}/access` | [`principal_access`](../services/control-plane/app/routes/rbac.py#L261) |
| GET | `/v1/rbac/resources` | [`resources`](../services/control-plane/app/routes/rbac.py#L421) |
| DELETE | `/v1/rbac/resources/{resource_id}` | [`deregister_schema`](../services/control-plane/app/routes/rbac.py#L608) |
| GET | `/v1/rbac/resources/{resource_id}` | [`read_resource`](../services/control-plane/app/routes/rbac.py#L597) |
| PATCH | `/v1/rbac/resources/{resource_id}` | [`move_resource`](../services/control-plane/app/routes/rbac.py#L485) |
| GET | `/v1/rbac/resources/{resource_id}/access` | [`effective_access`](../services/control-plane/app/routes/rbac.py#L763) |
| GET | `/v1/rbac/resources/{resource_id}/assignments` | [`assignments`](../services/control-plane/app/routes/rbac.py#L622) |
| PUT | `/v1/rbac/resources/{resource_id}/assignments` | [`assign`](../services/control-plane/app/routes/rbac.py#L702) |
| DELETE | `/v1/rbac/resources/{resource_id}/assignments/services/{principal_id}` | [`revoke_service`](../services/control-plane/app/routes/rbac.py#L753) |
| DELETE | `/v1/rbac/resources/{resource_id}/assignments/{app_group_id}` | [`revoke`](../services/control-plane/app/routes/rbac.py#L740) |
| POST | `/v1/rbac/resources/{resource_id}/provision` | [`provision`](../services/control-plane/app/routes/rbac.py#L770) |
| GET | `/v1/rbac/resources/{resource_id}/service-principals` | [`service_principals`](../services/control-plane/app/routes/rbac.py#L645) |
| GET | `/v1/rbac/resources/{resource_id}/user-principals` | [`user_principals`](../services/control-plane/app/routes/rbac.py#L656) |
| GET | `/v1/rbac/roles` | [`roles`](../services/control-plane/app/routes/rbac.py#L122) |
| POST | `/v1/rbac/sources/{name}/schemas` | [`register_schema`](../services/control-plane/app/routes/rbac.py#L558) |

### runs

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/runs` | [`list_runs`](../services/control-plane/app/routes/runs.py#L61) |
| POST | `/v1/runs` | [`create_run`](../services/control-plane/app/routes/runs.py#L286) |
| GET | `/v1/runs/{run_id}` | [`get_run`](../services/control-plane/app/routes/runs.py#L311) |
| GET | `/v1/runs/{run_id}/logs` | [`run_logs`](../services/control-plane/app/routes/runs.py#L318) |
| POST | `/v1/runs/{run_id}/open` | [`open_app`](../services/control-plane/app/routes/runs.py#L394) |
| GET | `/v1/runs/{run_id}/outputs` | [`run_outputs`](../services/control-plane/app/routes/runs.py#L334) |
| GET | `/v1/runs/{run_id}/outputs/{path:path}` | [`run_output`](../services/control-plane/app/routes/runs.py#L350) |
| POST | `/v1/runs/{run_id}/retry` | [`retry_run`](../services/control-plane/app/routes/runs.py#L404) |
| POST | `/v1/runs/{run_id}/stop` | [`stop_run`](../services/control-plane/app/routes/runs.py#L374) |

### scim

| Method | Path | Handler / source |
|---|---|---|
| GET | `/scim/v2/ResourceTypes` | [`resource_types`](../services/control-plane/app/routes/scim.py#L145) |
| GET | `/scim/v2/ResourceTypes/{type_id}` | [`resource_type`](../services/control-plane/app/routes/scim.py#L171) |
| GET | `/scim/v2/Schemas` | [`schemas`](../services/control-plane/app/routes/scim.py#L152) |
| GET | `/scim/v2/Schemas/{schema_id}` | [`schema`](../services/control-plane/app/routes/scim.py#L163) |
| GET | `/scim/v2/ServiceProviderConfig` | [`provider_config`](../services/control-plane/app/routes/scim.py#L135) |
| GET | `/scim/v2/{collection}` | [`list_resources`](../services/control-plane/app/routes/scim.py#L179) |
| POST | `/scim/v2/{collection}` | [`create_resource`](../services/control-plane/app/routes/scim.py#L205) |
| DELETE | `/scim/v2/{collection}/{pid}` | [`delete_resource`](../services/control-plane/app/routes/scim.py#L299) |
| GET | `/scim/v2/{collection}/{pid}` | [`get_resource`](../services/control-plane/app/routes/scim.py#L198) |
| PATCH | `/scim/v2/{collection}/{pid}` | [`update_resource`](../services/control-plane/app/routes/scim.py#L237) |
| PUT | `/scim/v2/{collection}/{pid}` | [`update_resource`](../services/control-plane/app/routes/scim.py#L237) |

### sql

| Method | Path | Handler / source |
|---|---|---|
| POST | `/internal/trino/allow` | [`allow`](../services/control-plane/app/routes/sql.py#L340) |
| POST | `/internal/trino/batch` | [`batch`](../services/control-plane/app/routes/sql.py#L378) |
| POST | `/internal/trino/batch-column-masks` | [`column_masks`](../services/control-plane/app/routes/sql.py#L422) |
| GET | `/internal/trino/jwks` | [`trino_jwks`](../services/control-plane/app/routes/sql.py#L334) |
| POST | `/internal/trino/layers/{layer}/allow` | [`layer_allow`](../services/control-plane/app/routes/sql.py#L345) |
| POST | `/internal/trino/layers/{layer}/batch` | [`layer_batch`](../services/control-plane/app/routes/sql.py#L383) |
| POST | `/internal/trino/layers/{layer}/batch-column-masks` | [`layer_column_masks`](../services/control-plane/app/routes/sql.py#L427) |
| POST | `/internal/trino/layers/{layer}/row-filters` | [`layer_row_filters`](../services/control-plane/app/routes/sql.py#L409) |
| POST | `/internal/trino/row-filters` | [`row_filters`](../services/control-plane/app/routes/sql.py#L404) |
| GET | `/v1/info` | [`info`](../services/control-plane/app/routes/sql.py#L223) |
| DELETE, GET | `/v1/sql/pages/{token}` | [`page`](../services/control-plane/app/routes/sql.py#L192) |
| GET | `/v1/sql/queries/{query_id}` | [`query_status`](../services/control-plane/app/routes/sql.py#L212) |
| POST | `/v1/sql/query` | [`query_request`](../services/control-plane/app/routes/sql.py#L254) |
| POST | `/v1/statement` | [`statement`](../services/control-plane/app/routes/sql.py#L144) |

### webhooks

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/webhooks` | [`list_webhooks`](../services/control-plane/app/routes/webhooks.py#L55) |
| DELETE | `/v1/admin/webhooks/{name}` | [`delete_webhook`](../services/control-plane/app/routes/webhooks.py#L98) |
| PUT | `/v1/admin/webhooks/{name}` | [`put_webhook`](../services/control-plane/app/routes/webhooks.py#L62) |
| GET | `/v1/admin/webhooks/{name}/deliveries` | [`deliveries`](../services/control-plane/app/routes/webhooks.py#L126) |
| POST | `/v1/admin/webhooks/{name}/test` | [`test_webhook`](../services/control-plane/app/routes/webhooks.py#L111) |
| GET | `/v1/alerts/channels` | [`alert_channels`](../services/control-plane/app/routes/webhooks.py#L182) |
| GET | `/v1/alerts/events` | [`alert_events`](../services/control-plane/app/routes/webhooks.py#L177) |
| GET | `/v1/alerts/rules` | [`list_rules`](../services/control-plane/app/routes/webhooks.py#L188) |
| POST | `/v1/alerts/rules` | [`create_rule`](../services/control-plane/app/routes/webhooks.py#L201) |
| DELETE | `/v1/alerts/rules/{rule_id}` | [`delete_rule`](../services/control-plane/app/routes/webhooks.py#L227) |
| PUT | `/v1/alerts/rules/{rule_id}` | [`update_rule`](../services/control-plane/app/routes/webhooks.py#L212) |

### workspace_templates

| Method | Path | Handler / source |
|---|---|---|
| GET | `/v1/admin/workspace-templates` | [`list_templates`](../services/control-plane/app/routes/workspace_templates.py#L61) |
| POST | `/v1/admin/workspace-templates` | [`create_template`](../services/control-plane/app/routes/workspace_templates.py#L66) |
| DELETE | `/v1/admin/workspace-templates/{template_id}` | [`delete_template`](../services/control-plane/app/routes/workspace_templates.py#L105) |
| GET | `/v1/admin/workspace-templates/{template_id}` | [`get_template`](../services/control-plane/app/routes/workspace_templates.py#L80) |
| PUT | `/v1/admin/workspace-templates/{template_id}` | [`update_template`](../services/control-plane/app/routes/workspace_templates.py#L88) |

### workspaces

| Method | Path | Handler / source |
|---|---|---|
| POST | `/v1/executions/token` | [`refresh_run_token`](../services/control-plane/app/routes/workspaces.py#L359) |
| GET | `/v1/me` | [`me`](../services/control-plane/app/routes/workspaces.py#L26) |
| PUT | `/v1/me/active-group` | [`select_active_group`](../services/control-plane/app/routes/workspaces.py#L130) |
| PUT | `/v1/me/active-layer` | [`select_active_layer`](../services/control-plane/app/routes/workspaces.py#L110) |
| GET | `/v1/pools` | [`list_pools`](../services/control-plane/app/routes/workspaces.py#L321) |
| GET | `/v1/workspaces` | [`list_workspaces`](../services/control-plane/app/routes/workspaces.py#L159) |
| POST | `/v1/workspaces` | [`create_workspace`](../services/control-plane/app/routes/workspaces.py#L172) |
| DELETE | `/v1/workspaces/{ws_id}` | [`delete_workspace`](../services/control-plane/app/routes/workspaces.py#L302) |
| GET | `/v1/workspaces/{ws_id}/config` | [`workspace_config`](../services/control-plane/app/routes/workspaces.py#L334) |
| POST | `/v1/workspaces/{ws_id}/open` | [`open_workspace`](../services/control-plane/app/routes/workspaces.py#L348) |
| POST | `/v1/workspaces/{ws_id}/start` | [`start_workspace`](../services/control-plane/app/routes/workspaces.py#L195) |
| POST | `/v1/workspaces/{ws_id}/stop` | [`stop_workspace`](../services/control-plane/app/routes/workspaces.py#L284) |

## Desktop pages and aliases

Every tab below requires its page gate before mounting, except reference pages marked none. Page visibility and object/function authority are separate.

| Tab kind | Page gate |
|---|---|
| `home` | `home` |
| `catalog` | `catalog` |
| `catalog-object` | `catalog` |
| `products` | `products` |
| `product` | `products` |
| `access-requests` | `requests` |
| `users` | `people` |
| `access` | `set-groups` |
| `workspaces` | `workspaces` |
| `workspace` | `workspaces` |
| `runs` | `runs` |
| `run` | `runs` |
| `data` | `storage` |
| `source` | `storage` |
| `schema` | `storage` |
| `data-object` | `storage` |
| `data-permissions` | `storage` |
| `admin-sources` | `storage` |
| `kafka` | `kafka` |
| `kafka-clusters` | `kafka` |
| `apis` | `apis` |
| `api` | `apis` |
| `pool` | `usage` |
| `folder` | `workloads` |
| `deploy` | `workloads` |
| `workload` | `workloads` |
| `service` | `set-services` |
| `services` | `set-services` |
| `admin-service-accounts` | `set-services` |
| `admin-layers` | `set-layers` |
| `app-profiles` | `set-profiles` |
| `admin-app-profiles` | `set-profiles` |
| `admin-notifications` | `set-alerts` |
| `admin-classifications` | `set-classes` |
| `privacy-classifications` | `set-classes` |
| `privacy-policies` | `set-policies` |
| `privacy-reviews` | `reviews` |
| `privacy-memberships` | `memberships` |
| `privacy-register` | `set-register` |
| `privacy-case` | `cases` |
| `case` | `cases` |
| `privacy-forms` | `set-forms` |
| `privacy-form` | `set-forms` |
| `privacy-roles` | `set-duties` |
| `repos` | `repos` |
| `admin-repos` | `repos` |
| `admin-runtimes` | `set-runtimes` |
| `secrets` | `secrets` |
| `docs` | `docs` |
| `admin-pools` | `set-pools` |
| `admin-workspace-templates` | `set-template` |
| `admin-network` | `set-network` |
| `admin-query-log` | `set-querylog` |
| `git-credentials` | `git-creds` |
| `packages` | `packages` |
| `package` | `packages` |
| `package-imports` | `imports` |
| `admin-audit` | `set-auditlog` |
| `admin-backups` | `set-backups` |
| `deadlines` | `deadlines` |
| `roles` | none: a reference page for everyone signed in |

## Other entry points

Gateway forward authentication, broker policy callbacks, SQL policy callbacks, native package indexes and keyed aliases are included above. They require their own admission, service authentication and live object checks. Framework documentation routes, startup/leader controllers and offline recovery/migration commands are additional boundaries; controller entry points must preserve the authority captured at admission.
