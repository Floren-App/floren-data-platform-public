# Kubernetes configuration contracts (version 1)

This document defines the first implementation boundary for the
[configuration guide](configuration.md). Contracts and API admission
and core layer reconciliation are implemented. The complete deployment remains
in progress; an accepted infrastructure operation does not mean a usable layer
exists until its readiness checks pass.

## Desired state and provisioning operations

Layers retain their existing stable 12-character IDs. Their display names are
editable; namespaces are derived from IDs (`fdp-layer-<id>`), never names or
client-provided namespace values. Namespace selection is not an authorization input.

Existing layers are unmanaged by default. No namespace, data, permission, or running
workload migration occurs just because the server is upgraded.

Administrator APIs:

- `GET /v1/admin/layers/{id}/infrastructure`: normalized desired state, generation,
  and the latest operation.
- `PUT /v1/admin/layers/{id}/infrastructure`: full desired-state replacement with an
  `expected_generation`; returns HTTP 202 and durable operation status.
- `GET /v1/admin/layers/{id}/operations`: latest 100 operation records.
- `POST /v1/admin/layers/{id}/infrastructure/retry`: request a new reconciliation
  generation of the same desired state after correcting an external prerequisite.
  Supply `expected_generation`; an unfinished operation still blocks a retry.

The desired-state schema is defined by `InfrastructureSpec` in
`services/control-plane/app/layer_infrastructure.py` and deployed in
`schemas/layer-infrastructure.schema.json`. Unknown properties are rejected.

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

The ordinary layer edit API preserves infrastructure settings. Existing JSON
configuration clients continue to work. The new contract is opt-in.

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
  trust before reporting Ready. Plain HTTP remains a legacy option using a
  layer-bound policy context and restricted coordinator ingress.
  The controller generates the layer-local node secret unless overridden.
- Storage: separate workspace and artifact volume class, size and access mode,
  and Retain policy. Cross-namespace PVC references are not accepted. The reserved
  object-storage artifact-secret setting is not implemented yet.
- Optional PostgreSQL: explicit image, CPU/memory, credential Secret, storage
  class/size and TLS. Provisioned as a single-instance layer data database with a
  retained PVC and a separate non-superuser engine login. Existing TLS, live native
  reload, database retention and network isolation are verified on Kubernetes;
  cert-manager Certificate/readiness logic has unit coverage. The controller checks
  native TLS with platform CA trust. Database image, credential reference and volume
  changes require an explicit migration; disabling storage or downgrading TLS is
  refused. Automatic catalog/credential wiring, native password rotation and HA are
  not implemented. See the chart's layer PostgreSQL instructions.
- Optional RustFS: explicit image supplied by the administrator, credential Secret,
  storage class/size, TLS. Its tested deployment profile is still to be implemented.

Managed layers require signing and internal-secret references. Infrastructure
images cannot use `latest` or an unversioned image name. These checks establish a
configuration contract; resource readiness and connector compatibility still need
controller-side validation against Kubernetes and the actual services.

## Ownership boundaries

| Component | Owns | Must not own |
|---|---|---|
| Helm | Platform installation, controller deployment, optional platform dependencies | Individual runs or UI-created layer instances |
| Layer controller | Generated layer prerequisites and infrastructure status | App-group grants or Terraform state |
| Runtime orchestrator | Admitted workload pods, services, workspace/runtime storage | Namespace creation, baseline policies, or layer readiness |
| Terraform | Declared application configuration through the validated API | Generated Kubernetes resources or runtime history |
| Issuer/certificate controller | Certificate issuance, private keys, renewal | Application grants or cross-layer secret access |

The controller will run separately from the control-plane deployment. Its
namespace-provisioning permissions must not be inherited by execution identities.
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
the controller reports Ready. A disposable Kubernetes integration verifies actual
cert-manager issuance, requested renewal, key rotation, kubelet projection and
Trino 484 reload without restarts. It also verifies CA replacement with overlapping
platform trust and rejection of the old root. Automatic renewal scheduling and
expiry/failure alerts remain unverified. The layer-local trust-bundle Secret key
is validated by a tokenless init container and converted to a public-CA-only Java
PKCS12 truststore used by each Trino JVM. It replaces default Java roots; include
all required roots and enable each connector's TLS/hostname checks explicitly.
Use a new versioned Secret reference and controlled rollout for CA changes; JVM
trust is not reloaded in place. Real PostgreSQL JDBC verified-TLS queries and
wrong-host/untrusted-CA rejection are integration-tested. TLS-enabled managed
layers also enable native Trino discovery/exchange TLS, bound to the layer's node
secret and environment, and disable their plaintext listener. Workers resolve a
headless coordinator Service to its pod IP before startup and restart when that
endpoint changes. DNS lookups are bounded; outages do not intentionally restart
healthy workers. Kubernetes integration verifies encrypted worker queries through
renewal and recovery after coordinator replacement. Legacy `disabled` TLS retains
authenticated HTTP. Full runtime trust delivery remains open.

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
group using this header. The first Terraform provider will use the dedicated OIDC
automation identity; richer scoped platform automation credentials can be added
without weakening workload identity boundaries.

Each resource's layer is supplied through its typed configuration API and remains
subject to existing layer and promotion validation. Never infer it from the active
UI page or silently use the human operator's latest selection.

## Admission and deletion

Starting a new workspace or job in a managed layer requires a Ready operation for
its current generation. Existing unmanaged layers keep their current behavior.
Already running jobs retain their existing admission snapshots.

Configuring infrastructure does not migrate running workloads. Managed
infrastructure cannot be disabled through an ordinary PUT. Layers with
infrastructure history cannot be deleted through the legacy layer-delete endpoint.
This deliberately fails closed until explicit retirement is implemented. No PVC,
bucket, database, or historical operation is removed implicitly.

## Migration and rollback sequence

1. Install API/schema additions with managed infrastructure disabled. Existing
   namespaces, volumes, URLs, grants, and execution remain unchanged.
2. Inventory source connections, running jobs, workspace volumes, artifact paths,
   credentials, and current layer mappings. Back up metadata and persistent data.
3. Rehearse new isolated layers on a disposable Kubernetes cluster. Require policy,
   identity, service, certificate, and storage readiness before admitting workloads.
4. Once reconciliation and execution integration are verified, stop new admissions
   to each migrating layer, allow admitted jobs to finish, and stop its workspaces.
5. Copy files/artifacts into private, layer-local storage; verify contents and
   access controls. Preserve originals as rollback data. Do not remount a shared
   cross-layer NFS export into the new namespaces.
6. Configure only that layer's connectors and credentials, then switch its runtime
   bindings after positive and negative access tests. Use a later explicit migration
   operation to record activation; do not edit database status fields by hand.
7. Retire the legacy path only after all layers and administration paths pass.

Before activation, rollback leaves the old runtime authoritative. After new data
writes begin, rollback requires a coordinated restore/reconciliation decision;
reverting a Helm release alone cannot undo database migrations or data changes.
Keep platform metadata queries on their protected administrative path throughout.
