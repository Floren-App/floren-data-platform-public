# Bring your own infrastructure

Every external dependency is reached through an open interface. This page lists what each
one must provide, the optional bundled alternative and what has been tested. Installation
steps are in the [Kubernetes guide](../kubernetes-install.md). Its
[`external-services.yaml`](../../deploy/helm/fdp/examples/external-services.yaml) example
turns every bundled service off.

## Identity provider

| Aspect | Details |
|---|---|
| Interface | OpenID Connect sign-in. SCIM 2.0 directory provisioning to `/api/scim/v2` |
| You provide | An issuer with an API audience in access tokens. A public client with PKCE for the browser and desktop app. A confidential client for OpenTofu automation. An administrator role |
| Bundled option | Keycloak (`keycloak.enabled`) |
| Tested | Keycloak, bundled and external over HTTPS |

FDP authenticates people with your IdP and keeps its own directory, provisioned over SCIM.
FDP groups bind IdP users, groups and roles to permissions, so your IdP stays the source
of truth for who someone is. Claim names are configurable (`oidc.rolesClaim`,
`oidc.groupsClaim`, `oidc.usernameClaim`). Any provider that issues standard OIDC tokens
and speaks SCIM 2.0 should work. Test your provider's SCIM behavior (filters, PATCH and
deprovisioning) before production use.

## Secret store

Secret values (data-source credentials, Git credentials, program API keys and certificates) live only
in the configured store. The metadata database holds references. Values are write-only through the API
and OpenTofu, and are never returned to clients.

| Store | Interface | Platform identity | Tested with |
|---|---|---|---|
| **OpenBao, bundled** (default all-in-one) | KV v2 | Kubernetes auth, configured automatically | OpenBao 2.7 server: self-initialization, policy, restart and Raft failover |
| OpenBao or HashiCorp Vault, your own | KV v2 | Token, Kubernetes auth or AppRole | OpenBao 2.7 and Vault 2.2 servers: token, AppRole and Kubernetes login, policy limits |
| Kubernetes Secrets | A dedicated namespace | The control plane's service account | Kubernetes |
| AWS Secrets Manager | AWS API | IRSA or EKS Pod Identity | An in-process AWS API emulator (moto) |
| Azure Key Vault | Azure API | Azure Workload Identity | SDK-level fakes |
| Google Secret Manager | Google API | GKE Workload Identity | SDK-level fakes |

**Bundled OpenBao** runs with Raft storage in its own namespace, reachable only from the control plane.
On first start it configures itself: a KV mount, an audit log, Kubernetes auth for the control plane and
a policy limited to that mount. The root token used for this is revoked immediately, and the chart
never stores a root token or unseal key. It unseals automatically, in one of two ways:

- **Kubernetes model** (default): a 32-byte seal key in a Kubernetes Secret, created once inside the
  cluster and never rewritten. As safe as your etcd encryption and Secret access controls.
- **KMS model**: your cloud KMS (AWS, Azure, Google, Oracle) or a transit key unseals OpenBao. No key is
  stored in the cluster.

HashiCorp Vault is supported as a server you run yourself. It is not open source (BSL), so FDP never
ships it. Dynamic database credentials require the OpenBao or Vault backend. See the
[chart README](../../deploy/helm/fdp/README.md#secret-stores) for every setting.

## Platform metadata database

| Aspect | Details |
|---|---|
| Interface | PostgreSQL (`FDP_DATABASE_URL`) |
| You provide | A database owned by a login role that is not a superuser. FDP creates its own tables |
| Bundled option | Single-instance PostgreSQL (`metadataDatabase.enabled`) |
| Optional | A role that may create databases, for private per-deployment Airflow databases (`FDP_AIRFLOW_METADATA_URL`) |

The metadata database holds configuration, permissions, run history, the catalog and audit
events. It holds no business data. Back it up together with the platform's signing keys.
See [operations](../kubernetes-operations.md#backup-and-restore).

## Data storage

FDP governs storage it does not own. Each location is an explicit
[storage integration](../storage-integrations.md):

| Type | You provide |
|---|---|
| PostgreSQL | A database, a login for the SQL engine and a separate login that sets up schema access |
| Iceberg tables | An S3-compatible bucket and an Iceberg REST catalog |
| S3 files | An S3-compatible bucket and a role FDP may assume through STS, to issue short-lived prefix-scoped credentials |

Use AWS S3 or any S3-compatible service that supports STS AssumeRole. Scoped file access
is tested against RustFS.

**Bundled option:** each managed [layer](../layers.md) can run its own PostgreSQL and
RustFS, with Iceberg on that storage, in the layer's namespace. These are single-instance
services on retained volumes, intended for evaluation and smaller installations.

## SQL engine

Trino is part of FDP rather than something you bring, because it is where data
permissions are enforced. Every query from Python, R, dbt, dashboards and the SQL editor
passes through it, checked against FDP's policy for each referenced schema. Each managed
layer gets its own Trino, sized and scaled by the platform. Database and storage
credentials are configured on Trino's catalogs, never in workloads. See
[SQL execution](../query-execution.md).

## Kubernetes and networking

| Requirement | Why |
|---|---|
| Kubernetes 1.27 or later | Pod Security labels, server-side apply |
| A network plugin that enforces NetworkPolicy | Layer isolation is built from network policies |
| A default StorageClass | Workspace, artifact and bundled-database volumes |
| TLS certificates | An existing Secret, or cert-manager |

`make preflight` verifies these on a cluster before installation, including that a deny
policy actually blocks traffic. The full installation test runs on k3s. Other conformant
distributions with an enforcing CNI are expected to work but are not yet covered by the
automated tests. IPv6 is not supported yet.

## Everything else

| Dependency | Interface | Notes |
|---|---|---|
| Source control | Git over HTTPS | Any Git server. Workloads deploy from branches you register |
| Container images | Any OCI registry | Pin versions or digests. Private registries use one pull Secret |
| Python and R packages | The platform package repository | Proxies PyPI and CRAN, or runs air-gapped with no upstream |
| Monitoring | Prometheus | Optional ServiceMonitor and certificate metrics |
| Backups | Your tooling | `pg_dump` for metadata, volume snapshots, your secret store's own backups |

## Moving an installation

Portability follows from the points above. To move FDP to another cluster or cloud:

1. Restore the metadata database together with the platform Secret (signing keys).
2. Point the new installation at the same identity provider and secret store, or migrate
   them with their own tooling.
3. Re-register storage endpoints if their addresses change. The data itself does not move
   through FDP.
4. Re-apply Helm values and OpenTofu configuration. A clean `tofu plan` confirms the result.
