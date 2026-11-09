# Feature boundaries

These are product boundaries, not promises about every Kubernetes installation.
Confirm the source revision, configuration and integration checks for your deployment.

| Capability | Current boundary |
|---|---|
| Audit export | Audit trail and SQL data access to stdout, syslog (TLS), HTTPS (generic, Splunk, Elastic) and OTLP, hash-chained and HMAC-signed |
| Secret stores | Bundled OpenBao (Kubernetes or KMS auto-unseal), your OpenBao/Vault, Kubernetes Secrets, AWS, Azure and Google secret managers; one store per installation |
| Workspaces | VS Code, RStudio and SQL editors; private subdirectory storage; root/direct-repository config discovery |
| Layer isolation | Managed layers: own namespace, default-deny network policies and layer-local services; requires a CNI that enforces policy. Unmanaged layers are an authorization boundary only |
| Data IAM | Additive schema/source grants plus explicit table, column, file and directory targets. Fine SQL grants use governed Trino; raw native credentials are refused. Column masks and row filters remain enforced |
| Backup and recovery | Optional backup plans for the control plane and PostgreSQL, S3 files and Iceberg integrations, each with its own destination, schedule and retention; encrypted archives with references into the selected vault; offline restore, quarantine and reconciliation. Nothing runs until a plan is enabled. Workspace volumes, packages, Kafka messages, the IdP, the vault, Git and Terraform state need their own arrangements; see [recovery](platform-recovery.md) |
| Metadata upgrades | Explicit serialized Alembic migrations; API startup refuses a pending schema; see [upgrades](metadata-upgrades.md) |
| Native DDL | Admin may receive native privileges that also permit native grants on some engines; FDP IAM assignment remains protected |
| Workload IAM | Nested folders, additive inheritance, built-in managed identities and fixed/caller data modes |
| Catalog | Governed resources, search, lineage, quality and freshness/run SLA evidence; uncertified objects remain available |
| Data products | Managed bundles of tables, files, dashboards and other objects, with access options (masks and row filters), requests, approvals and compliance forms; managed through the app, the API and the `fdp_data_product` resource |
| Lineage | Program observations and native dbt/Airflow evidence; completeness depends on executed/instrumented paths and captured artifacts |
| SLA enforcement | Configurable evidence and input gates; external notification delivery is not a complete incident-management system |
| Bundled services | Keycloak, PostgreSQL and RustFS are optional and single-instance, not highly available; bring your own for HA |
| Provider distribution | Signed GitHub releases of Floren-App/terraform-provider-fdp, installed into a filesystem mirror (`scripts/install-provider.sh`); not listed in a public registry |
| Documentation versions | This site follows main; historical Markdown is available at source commits |
| Kafka | Phase 1 (managed Kafka per layer, governed topics, stream workloads) is implemented and verified against a real broker; its live Kubernetes acceptance (operator reconciliation, network policy enforcement, volume lifecycle) is still open. Connectors, schemas and external access are planned; see [Kafka](kafka-operations.md) |
| AI agent | Not available yet; planned (see the roadmap) |

The [installation guide](kubernetes-install.md#what-is-not-supported-yet) and
[SQL execution guide](query-execution.md) describe additional backend limitations. See
[bring your own infrastructure](architecture/bring-your-own.md) for what has been tested.
