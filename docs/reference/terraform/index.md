# Terraform provider schema

Generated from the provider schemas; do not edit these tables manually.

For installation, authentication, examples, imports and state protection, see the [Terraform guide](../../terraform.md).

## Provider configuration

Configures Floren Data Platform through its API. Authenticate with a token, or with OAuth client credentials of an automation identity (for example the chart's fdp-automation client).

| Attribute | Type | Behavior | Description |
|---|---|---|---|
| `ca_file` | `basetypes.StringType` | Optional | PEM bundle to trust for the API's TLS certificate. Env: FDP_CA_FILE. |
| `client_id` | `basetypes.StringType` | Optional | Env: FDP_CLIENT_ID. |
| `client_secret` | `basetypes.StringType` | Optional, Sensitive | Env: FDP_CLIENT_SECRET. |
| `endpoint` | `basetypes.StringType` | Optional | API base URL, e.g. https://fdp.example.com/api. Env: FDP_ENDPOINT. |
| `owner_group_id` | `basetypes.StringType` | Optional | App group that owns created objects. Env: FDP_OWNER_GROUP_ID. |
| `token` | `basetypes.StringType` | Optional, Sensitive | Bearer token. Env: FDP_TOKEN. |
| `token_url` | `basetypes.StringType` | Optional | OAuth token endpoint for client credentials. Env: FDP_TOKEN_URL. |

## Resources

- [fdp_api](fdp_api.md)
- [fdp_api_operation](fdp_api_operation.md)
- [fdp_app_group](fdp_app_group.md)
- [fdp_app_group_member](fdp_app_group_member.md)
- [fdp_catalog_governance](fdp_catalog_governance.md)
- [fdp_compute_pool](fdp_compute_pool.md)
- [fdp_data_policy](fdp_data_policy.md)
- [fdp_data_source](fdp_data_source.md)
- [fdp_default_layer](fdp_default_layer.md)
- [fdp_default_warehouse](fdp_default_warehouse.md)
- [fdp_egress_allowlist](fdp_egress_allowlist.md)
- [fdp_folder](fdp_folder.md)
- [fdp_kafka_cluster](fdp_kafka_cluster.md)
- [fdp_kafka_topic](fdp_kafka_topic.md)
- [fdp_layer](fdp_layer.md)
- [fdp_metric](fdp_metric.md)
- [fdp_package](fdp_package.md)
- [fdp_package_allowlist](fdp_package_allowlist.md)
- [fdp_query_log_settings](fdp_query_log_settings.md)
- [fdp_repository](fdp_repository.md)
- [fdp_role_assignment](fdp_role_assignment.md)
- [fdp_runtime](fdp_runtime.md)
- [fdp_schema](fdp_schema.md)
- [fdp_secret](fdp_secret.md)
- [fdp_service_account](fdp_service_account.md)
- [fdp_sla_policy](fdp_sla_policy.md)
- [fdp_workload](fdp_workload.md)
- [fdp_workspace_settings](fdp_workspace_settings.md)
- [fdp_workspace_template](fdp_workspace_template.md)

## Data sources

- [fdp_layer](data_fdp_layer.md)
- [fdp_principal](data_fdp_principal.md)
