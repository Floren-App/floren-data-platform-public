# Feature boundaries

These are product boundaries, not promises about every Kubernetes installation.
Confirm the source revision, configuration and integration checks for your deployment.

| Capability | Current boundary |
|---|---|
| Workspaces | VS Code, RStudio and SQL editors; private subdirectory storage; root/direct-repository config discovery |
| Layer isolation | Managed namespaces, network policies and layer-local services; requires a CNI that enforces policy |
| Data IAM | Schema roles include current/future objects; individual table grants are not the initial permission model |
| Native DDL | Admin may receive native privileges that also permit native grants on some engines; FDP IAM assignment remains protected |
| Workload IAM | Nested folders, additive inheritance, built-in managed identities and fixed/caller data modes |
| Catalog | Governed resources, search, lineage, quality and freshness/run SLA evidence; uncertified objects remain available |
| Multi-asset product bundles | Not yet a separately managed package resource; keep related asset descriptions/domain/tags consistent |
| Lineage | Program observations and native dbt/Airflow evidence; completeness depends on executed/instrumented paths and captured artifacts |
| SLA enforcement | Configurable evidence and input gates; external notification delivery is not a complete incident-management system |
| Infrastructure | Bundled PostgreSQL/RustFS are not an automatic HA data service; see installation limitations |
| Provider distribution | Source/local mirror supported; do not assume a published OpenTofu registry package |
| Documentation versions | This site follows main; historical Markdown is available at source commits |
| Kafka / agent framework | Not required platform components; no generic agent control plane or managed Kafka service is promised |

The [installation guide](kubernetes-install.md#what-is-not-supported-yet) and
[query execution guide](query-execution.md) describe additional backend limitations.
Plans and operational progress logs are intentionally excluded from the public site.
