# System components

FDP separates the control plane, isolated execution layers and user-facing clients.
Its metadata describes access and deployments. Business datasets stay in the storage
you register. See [design principles](index.md) for the reasoning and
[core concepts](../concepts.md) for the terminology.

## Request and execution paths

```text
Browser / desktop ──OIDC──> Identity provider
        │ HTTPS
        ▼
Gateway ──authorization──> Control plane ──> Metadata PostgreSQL (yours or bundled)
   │                            │          Secret store (OpenBao or Kubernetes Secrets)
   │                            │          Git repositories
   │                            ▼
   │                     Layer orchestration
   │                            │
   ├──> Private workspace       ├──> Jobs / published apps / Airflow
   │                            │              │ managed execution context
   └──> Published app           └──> Governed SQL / managed APIs / files
                                              │
                                 Registered storage: PostgreSQL,
                                 S3-compatible buckets, Iceberg
```

## Control-plane responsibilities

The FastAPI service implements resource APIs, directory/SCIM handling, authorization,
workspace/run admission, workload GitOps, metadata, lineage and SLA evaluation.
Multiple replicas can serve requests; a PostgreSQL advisory lock elects one leader
for coordinated background work. Metadata persistence, migrations and retained
signing keys are essential to recovery.

Traefik routes the browser and desktop API, editor and application requests through the
appropriate authentication checks. Published apps and editors open separately from
the main app, using short-lived signed opening tickets.

The IdP authenticates people and automation. SCIM provisions directory identities
and memberships. FDP-owned app groups bind those identities to resource roles;
changing an IdP group does not directly mutate arbitrary native database grants.
See [RBAC](../rbac.md) and [active groups](../active-groups.md).

## Managed layers

A managed layer has its own namespace, default-deny network policies, service
identities, resource quota, artifact storage and SQL engine. Trino provides the SQL
execution path for PostgreSQL and Iceberg. Optional PostgreSQL and RustFS provide
layer-local storage. External storage is always an explicit storage integration.

Workspaces and their jobs belong to a layer. Their private volumes expose only the
project subdirectory. Published runs use their layer's artifact storage, separate
from workspace files. Shared infrastructure databases hold platform and Airflow state,
never business data.

Network isolation requires an enforcing CNI, correct pod/service/node CIDRs and
admitted egress. Application authorization and network boundaries complement each
other; neither substitutes for the other. See [layers](../layers.md).

## Compute and identity

An editor is a relatively small persistent session. A run is admitted to a named
compute pool with defined CPU/memory and concurrency limits. Queued work waits for
capacity. A published script is callable on demand; an app or Airflow deployment
may keep a serving process running. Their lifecycle is not interchangeable.

Each workload has a built-in managed identity. Fixed-mode executions use that
identity unless an explicitly authorized alternate identity is configured.
Caller-mode shared apps delegate data requests with viewer context. The data
permission check must happen for each request; a shared privileged connection does
not become viewer-scoped through a displayed username.

Queries enter the governed SQL path with an execution context. Permissions,
layer boundaries, source/schema access and append-only restrictions are checked
before the engine touches data. Engine/catalog/storage credentials remain
infrastructure credentials. Native DDL privilege limitations are documented in
[query execution](../query-execution.md) and [RBAC](../rbac.md).

## Metadata and observability

The catalog combines resource metadata with observed SQL/file/API dependencies and
native dbt/Airflow evidence. Lineage presents the inspected object's neighborhood,
collapsing schemas/projects/orchestrators until detail is requested. Orchestration
relationships can be overlaid separately from data dependencies.

Quality definitions, observations, dbt test results and freshness evidence feed
SLA evaluation. Missing evidence is not success. SQL query history and run history
record execution context and outcomes according to configured retention. The
[metadata SQL interface](../metadata-sql.md) exposes authorized metadata operations
through validation rather than unrestricted table mutation.

## Installation and configuration ownership

Helm (directly or through ArgoCD) installs the control plane and its infrastructure
configuration. OpenTofu and the UI manage application objects through the same API.
Workload repositories hold code and `fdp.json`, for example with separate Test and Prod
branches bound to different layers.
See [configuration ownership](../configuration.md), [GitOps](../gitops.md) and
[operations](../kubernetes-operations.md).

Kubernetes Secrets and OpenBao are the supported secret stores; see
[bring your own infrastructure](bring-your-own.md). Public TLS, internal
trust, client certificates, signing keys and database credentials each have distinct
rotation/backup requirements. See [secrets and certificates](../secrets-and-certificates.md).
