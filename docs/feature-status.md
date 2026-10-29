# Feature boundaries

These are product boundaries, not promises about every Kubernetes installation.
Confirm the source revision, configuration and integration checks for your deployment.

| Capability | Current boundary |
|---|---|
| Audit export | Audit trail and SQL data access to stdout, syslog (TLS), HTTPS (generic, Splunk, Elastic) and OTLP, hash-chained and HMAC-signed |
| Secret stores | Bundled OpenBao (Kubernetes or KMS auto-unseal), your OpenBao/Vault, Kubernetes Secrets, AWS, Azure and Google secret managers; one store per installation |
| Workspaces | VS Code, RStudio and SQL editors; private subdirectory storage; root/direct-repository config discovery |
| Layer isolation | Managed layers: own namespace, default-deny network policies and layer-local services; requires a CNI that enforces policy. Unmanaged layers are an authorization boundary only |
| Data IAM | Schema roles include current/future objects; individual table grants are not the initial permission model |
| Native DDL | Admin may receive native privileges that also permit native grants on some engines; FDP IAM assignment remains protected |
| Workload IAM | Nested folders, additive inheritance, built-in managed identities and fixed/caller data modes |
| Catalog | Governed resources, search, lineage, quality and freshness/run SLA evidence; uncertified objects remain available |
| Multi-asset product bundles | Not yet a separately managed package resource; keep related asset descriptions/domain/tags consistent |
| Lineage | Program observations and native dbt/Airflow evidence; completeness depends on executed/instrumented paths and captured artifacts |
| SLA enforcement | Configurable evidence and input gates; external notification delivery is not a complete incident-management system |
| Bundled services | Keycloak, PostgreSQL and RustFS are optional and single-instance, not highly available; bring your own for HA |
| Provider distribution | Signed GitHub releases of Floren-App/terraform-provider-fdp, prepared for the OpenTofu and Terraform registries; listing there is a separate submission |
| Documentation versions | This site follows main; historical Markdown is available at source commits |
| Kafka / agent framework | Not required platform components; no generic agent control plane or managed Kafka service is promised |

The [installation guide](kubernetes-install.md#what-is-not-supported-yet) and
[SQL execution guide](query-execution.md) describe additional backend limitations. See
[bring your own infrastructure](architecture/bring-your-own.md) for what has been tested.
