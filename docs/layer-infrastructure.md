# Layer infrastructure reference

A managed [layer](layers.md) is described by a desired-state specification that the layer
controller reconciles into Kubernetes resources. This page describes that specification,
its API and the ownership boundaries between the components involved. An accepted
infrastructure operation does not mean the layer is usable: it is usable once its
readiness checks pass.

## Desired state and provisioning operations

Layers have stable 12-character IDs. Their display names are editable. Namespaces are
derived from IDs (`fdp-<installation>-layer-<id>`), never from names or client-provided
values. Namespace selection is not an authorization input.

A layer without infrastructure is unmanaged. Enabling infrastructure is an explicit
operation. Upgrading the platform never converts a layer.

Administrator APIs:

- `GET /v1/admin/layers/{id}/infrastructure`: normalized desired state, generation,
  and the latest operation.
- `PUT /v1/admin/layers/{id}/infrastructure`: full desired-state replacement with an
  `expected_generation`; returns HTTP 202 and durable operation status.
- `GET /v1/admin/layers/{id}/operations`: latest 100 operation records.
- `POST /v1/admin/layers/{id}/infrastructure/retry`: request a new reconciliation
  generation of the same desired state after correcting an external prerequisite.
  Supply `expected_generation`; an unfinished operation still blocks a retry.

The desired-state schema is published as
[`schemas/layer-infrastructure.schema.json`](../schemas/layer-infrastructure.schema.json).
Unknown properties are rejected. With OpenTofu, use the `infrastructure` block of
[`fdp_layer`](reference/terraform/fdp_layer.md).

An update locks the layer row and compares its generation. Stale updates return
409, preventing two automation clients from silently overwriting each other.
An identical update at the current generation returns the existing operation.
Changed configuration increments the generation and creates an operation.
Only one unfinished operation is admitted per layer. Updating after a failed
operation is allowed. Clients must read state after a timeout before retrying.

Operations use these phases:

| Phase | Meaning |
|---|---|
| Pending | Desired state persisted, waiting for a controller |
| Provisioning | Controller is creating or reconciling prerequisites |
| InfrastructureReady | Trino foundation is ready; SQL/workspaces may be admitted, jobs/apps still need Ready |
| Ready | This generation is verified ready for execution |
| Failed | Reconciliation failed; reason/message explain the failed prerequisite |
| Superseded | Historical operation replaced by a newer desired generation |
| Deleting | Explicit retirement is removing permitted runtime infrastructure |

The API creates Pending and unmanaged Ready operations. The layer controller owns
provisioning, failure and managed Ready transitions, and periodically repairs drift.
Clients cannot submit status,
phase, namespace, readiness, or runtime credentials through the desired-state API.
Controller errors must be sanitized; Kubernetes responses or secret values must
not be copied into operation messages.

The ordinary layer edit API preserves infrastructure settings.

## Infrastructure settings

- Trino: explicit image version/digest; the coordinator's CPU and memory
  (`trino.coordinator`, at least 1Gi); the workers' warehouse and their minimum and maximum
  (`trino.workers`: `warehouse`, `min` ≥ 1, `max`); optional node-secret override,
  catalog Secret and TLS policy. Workers get the warehouse's CPU and memory, each
  takes one of its slots, and the minimum always runs. JVM heaps are 70% of each
  node's memory. A worker warehouse needs at least 1 GB memory (4 GB or more is
  recommended), must exist when the layer is configured, and cannot be deleted or
  shrunk below that while a layer uses it. The layer quota must hold the coordinator
  plus the maximum number of workers. Between min and max the platform scales
  with load: `queries_per_worker` (default 4) running plus waiting queries per
  worker adds workers at once, as far as the warehouse has free slots;
  load below that for `scale_down_after_seconds` (default 300) removes one worker
  at a time. A removed worker first finishes its running queries, for at most
  `drain_seconds` (default 600; its pod's termination grace period). The layer
  infrastructure state shows `trino_workers`: workers running, the load that
  decided it, and `waiting_for_warehouse` when the warehouse had no free slot. Specs saved before warehouses sized workers
  keep their CPU and memory as the coordinator's and need a worker warehouse on
  their next update. JWT configuration uses
  public JWKS; the private signing key stays in the platform. Existing/cert-manager
  TLS policies configure layer HTTPS; the controller waits for verified platform
  trust before reporting Ready. Plain HTTP is also possible, using a layer-bound
  policy context and restricted coordinator ingress.
  The controller generates the layer-local node secret unless overridden.
- Storage: separate workspace and artifact volume class, size and access mode,
  and Retain policy. Cross-namespace PVC references are not accepted.
- Optional PostgreSQL: explicit image, CPU/memory, credential Secret, storage
  class/size and TLS. Provisioned as a single-instance layer data database with a
  retained PVC and a separate non-superuser engine login. The controller checks
  native TLS with platform CA trust. Database image, credential reference and volume
  changes require an explicit migration. Disabling storage or downgrading TLS is
  refused. Native password rotation and high availability are not provided. See the
  chart's layer PostgreSQL instructions.
- Optional RustFS: explicit image, credential Secret, storage class/size, buckets and TLS.
  Iceberg on layer storage requires both the layer PostgreSQL and RustFS.

Managed layers require signing and internal-secret references. Infrastructure
images cannot use `latest` or an unversioned image name. Readiness is verified by the
controller against the running services, not inferred from the specification.

## Ownership boundaries

| Component | Owns | Must not own |
|---|---|---|
| Helm | Platform installation, controller deployment, optional platform dependencies | Individual runs or UI-created layer instances |
| Layer controller | Generated layer prerequisites and infrastructure status | App-group grants or Terraform state |
| Runtime orchestrator | Admitted workload pods, services, workspace/runtime storage | Namespace creation, baseline policies, or layer readiness |
| Terraform | Declared application configuration through the validated API | Generated Kubernetes resources or runtime history |
| Issuer/certificate controller | Certificate issuance, private keys, renewal | Application grants or cross-layer secret access |

The controller runs separately from the control-plane deployment. Its
namespace-provisioning permissions are never inherited by execution identities.
Runtime access is granted through scoped namespace Roles. Protected labels,
service-account selection, namespaces, and credentials are server-derived.

## Secret, certificate, and trust references

`SecretKeyRef` contains a name and key, never a value or namespace. It resolves
inside the object's own namespace. Resolving a reference must not copy a secret
from another layer. Certificate private keys and CA signing keys remain outside
the desired-state database and Terraform state.

Certificate policy modes:

- `disabled`: compatibility mode; no issuer, certificate, or trust references.
- `existing`: existing TLS Secret (`tls.crt` and `tls.key`), no issuer.
- `cert_manager`: TLS Secret name and an explicit Issuer/ClusterIssuer reference.

Issuance duration and renewal lead time are explicit and validated. Layer Trino
uses a derived namespace-qualified service SAN and local init/sidecar validation
before installing a certificate/key pair. Platform CA trust verifies HTTPS before
the controller reports Ready. Renewed certificates are reloaded by Trino without a
restart. Certificate state is monitored as described in
[operations](kubernetes-operations.md#certificate-monitoring-and-issuer-recovery).
The layer-local trust-bundle Secret key
is validated by a tokenless init container and converted to a public-CA-only Java
PKCS12 truststore used by each Trino JVM. It replaces default Java roots; include
all required roots and enable each connector's TLS/hostname checks explicitly.
Use a new versioned Secret reference and controlled rollout for CA changes; JVM
trust is not reloaded in place. Real PostgreSQL JDBC verified-TLS queries and
wrong-host/untrusted-CA rejection are integration-tested. TLS-enabled managed
layers also enable native Trino discovery/exchange TLS, bound to the layer's node
secret and environment, and disable their plaintext listener. Workers resolve a
headless coordinator Service to its pod IP before startup and restart when that
endpoint changes. DNS lookups are bounded, so a DNS outage does not restart healthy
workers. With TLS `disabled`, layers use authenticated HTTP.

The Helm chart implements public certificate references/issuance and platform PEM
trust mounts. See [certificate configuration](../deploy/helm/fdp/README.md).

## Automation authentication and explicit ownership

Use a dedicated OIDC automation principal with the required Floren Data Platform app-group
bindings. Obtain its access token through the external IdP's supported flow. For
SCIM deployments the automation principal must also be provisioned in the directory.
Do not give workload/session credentials platform administration authority.

Send `X-FDP-Active-Group: <authorized-app-group-id>` on automation API calls.
The server checks that the authenticated principal can select that group. This
sets the primary group for that request without changing the persisted desktop
selection. Existing creation authorization then checks the requested group's grants
and records it as the owner. Secondary grants do not authorize creation.

Workload and service-account execution credentials cannot override their primary
group using this header. The OpenTofu provider uses the dedicated OIDC automation
identity.

Each resource's layer is supplied through its typed configuration API and remains
subject to existing layer and promotion validation. Never infer it from the active
UI page or silently use the human operator's latest selection.

## Admission and deletion

Starting a new workspace or job in a managed layer requires a Ready operation for
its current generation. Already running jobs retain their admission snapshots.

Configuring infrastructure does not migrate running workloads. Managed
infrastructure cannot be disabled through an ordinary PUT. A layer with infrastructure
is removed through explicit [retirement](kubernetes-operations.md#retiring-and-deleting-a-layer).
No PVC, bucket, database or historical operation is removed implicitly.

## Converting an unmanaged layer

Enabling infrastructure on a layer that already holds work does not move files,
artifacts or credentials. Before converting one:

1. Back up platform metadata and persistent data, and inventory the layer's
   connections, workspaces, artifacts and running jobs.
2. Stop new admissions to the layer, let admitted jobs finish and stop its workspaces.
3. Apply the infrastructure and wait for Ready. Copy files and artifacts into the
   layer-local storage, and verify contents and access controls. Keep the originals.
4. Configure the layer's connectors and credentials, then test positive and negative
   access before reopening the layer.

After new data writes begin, rolling back requires a coordinated restore. Reverting a
Helm release alone cannot undo database migrations or data changes.

Kafka is a built-in capability of every layer. Configure `kafka.running` to control
broker compute without removing the cluster or its retained data. Legacy Kafka
`enabled` input maps to running/stopped. See [Kafka operations](kafka-operations.md).
