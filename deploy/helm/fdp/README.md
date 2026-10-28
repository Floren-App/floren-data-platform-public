# Floren Data Platform Helm chart

Installs the Floren Data Platform control plane, gateway and optional components. Every supporting
service can be bundled or brought from outside: Keycloak or any OIDC/SCIM identity provider, PostgreSQL
for platform metadata, OpenBao or another secret store, and per-layer PostgreSQL and RustFS. The optional
layer controller provisions isolated layers with their own Trino, workspace volumes and artifact storage.
Start with the [Kubernetes installation guide](../../../docs/kubernetes-install.md).

The shared platform/legacy data path currently needs an existing NFS export through
`dataVolume.nfs.server` and `dataVolume.nfs.path`. Both namespace-local claims bind
to that export. The PVs and PVCs carry Helm `keep` annotations, and PV reclaim policy
is `Retain`; uninstall therefore leaves storage for explicit operator recovery or
cleanup. This is separate from private workspace PVCs and managed layer artifacts.

## Layer data-source networking

Enable `layerController.enabled` to reconcile managed layers. Each layer starts
with default-deny ingress and egress. DNS and specific platform paths are allowed
separately. External data-source access requires the installation's **complete**
Pod, Service and node/host-network ranges:

```yaml
layerController:
  enabled: true
  network:
    podCidrs: ["10.42.0.0/16"]
    serviceCidrs: ["10.43.0.0/16"]
    nodeCidrs: ["192.168.10.0/24"]
    protectedCidrs: ["172.20.5.10/32"] # example external platform metadata database
```

These are example addresses, not cluster defaults. Use the ranges assigned to
your cluster, include node subnets used by autoscaling, and include both address
families on a dual-stack cluster. Reconfigure these exclusions before expanding
cluster networks. Use `protectedCidrs` for platform-only endpoints outside those
ranges. The controller validates CIDR syntax; it cannot prove operator-supplied
ranges describe every network in a cluster. They are an installation prerequisite.

Each storage integration's existing `egress` list supplies the permitted external
destination CIDRs. The controller reconciles policies only inside that source's
layer. PostgreSQL and Iceberg connections are available to that layer's Trino
pods, on the configured database/catalog/S3 ports. File S3 access is available only
to pods carrying the source's server-assigned grant label, on its S3 endpoint port.
Workloads do not receive STS provisioning credentials or direct SQL-engine
credentials. The ordinary runtime service account cannot modify network policies.

Every broad CIDR grant excludes the configured cluster networks, node addresses,
loopback, link-local and instance-metadata ranges. A source entirely inside one of
those protected ranges is rejected. Missing boundary settings or invalid grants
produce empty source policies and a Failed operation with a diagnostic reason;
correct the configuration and retry layer provisioning. This avoids leaving an
older additive allow in place. Sources with an empty `egress` list get no data
network access. Removed sources have their policies cleared on reconciliation.
Existing Ready layers reconcile approximately every 60 seconds.

This path supports external IP/CIDR destinations; it does not resolve DNS names
into trusted grants. Optional layer PostgreSQL uses generated same-namespace
pod-selector rules, as does optional layer RustFS (see below).
Use a CNI that enforces NetworkPolicy and verify service-DNAT behavior before
production use. `make test-layer-egress` checks the IPv4 rules on disposable k3s
1.33/kube-router without using the current Kubernetes context. IPv6 exclusions
are unit-tested; a dual-stack CNI test remains pending. Layer Trino HTTPS is
supported as described below, including native worker discovery/exchange TLS.
Full runtime trust delivery remains pending. Legacy HTTP Trino's JWT behavior is documented in the
implementation log, with policy-context and ingress checks providing its boundaries.

## Public certificates

For an existing certificate, provision the namespace-local Secret
`fdp-public-tls` with `tls.crt` (including its chain) and `tls.key`, and set
`tls.mode: existing`. See `examples/tls-existing.yaml`.

For automated issuance, use `examples/tls-cert-manager.yaml` and supply an existing
Issuer or ClusterIssuer. cert-manager and its CRDs must already be installed. The
chart creates a Certificate with SANs derived from `publicUrl`,
`apiGateway.publicUrl`, bundled Keycloak's public hostname, and
`tls.additionalDnsNames`. Private key rotation is Always;
duration and renewal lead time are configurable. No CA signing key belongs in Helm
values or the platform metadata database.

The gateway mounts the TLS Secret and generated dynamic configuration together as
a projected volume. File-provider watching is enabled. Actual renewal/reload
behavior must still be verified on the reference cluster before production use;
rendering manifests is not a certificate rotation test. Monitor Certificate Ready,
expiry, and renewal failures. Keep API gateway client-certificate validation in
the application, which selects the trusted client CA for each API.

`tls.mode: disabled` preserves the existing behavior for externally terminated TLS.
It does not provision a trusted public certificate. An external gateway must own
certificate issuance, trusted termination, and renewal in that configuration.

## Private CA trust

Set `trust.caBundleSecret` to an existing Secret containing `ca-bundle.pem`. Include
the required public roots and private CA certificates; do not include private keys.
The control plane mounts it read-only and uses it for Python HTTP/TLS clients.
New requests use verified TLS; trust is not bypassed.

Layer Trino HTTPS verifies against this bundle in the control plane and controller.
The SQL HTTP client detects CA bundle changes, creates a new verified connection
pool, and drains old requests before closing their pool. Include overlapping old
and new roots while rotating issuers. Invalid or unreadable trust bundles fail closed.

Workloads receive only the public bundle, never the Secret: the controller copies it into a
`fdp-trust` ConfigMap in each managed layer namespace, and the chart copies it into the
shared runtime namespace (`runtime.namespace`, refreshed on `helm upgrade`). Jobs, apps,
workspaces and Airflow mount it read-only at `/etc/fdp/trust/ca-bundle.pem` and get
`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `AWS_CA_BUNDLE`, `GIT_SSL_CAINFO`
and `NODE_EXTRA_CA_CERTS` pointing at it, which covers Python, R (curl, httr2,
`download.file`), Git, curl and Node. RStudio sessions receive the variables through
`Renviron.site`. `make test-image-trust` checks each client trusts a private CA with the
bundle and refuses it without. The bundle replaces the image's default roots, so include the
public roots your workloads need.

A PEM bundle does not configure Java trust stores. Layer-local Trino Java trust is configured
separately below. CA replacement must overlap old/new trust; Java clients require a
controlled rollout.

## Layer Trino HTTPS

The layer infrastructure API supports `trino.tls.mode: existing` with a
namespace-local `secret_name` containing standard `tls.key` and `tls.crt` PEM data.
The leaf certificate must cover the exact endpoint
`fdp-trino.<layer-namespace>.svc`. No private key is copied into the platform
or read by the controller. Configure the issuing CA in the platform's
`trust.caBundleSecret`; include public roots used by other platform HTTP clients.

Alternatively use `mode: cert_manager` with `secret_name` and `issuer` (name,
Issuer/ClusterIssuer kind, fixed cert-manager.io group). cert-manager and its CRDs
must already exist, and a namespace-scoped Issuer must exist in that layer. The
controller creates a Certificate with the required DNS SAN, configurable duration
and renewal interval, and `rotationPolicy: Always`. It waits for issuance, rollout,
and an HTTPS request that validates the certificate chain and hostname and confirms
Trino has finished initializing. The disposable Kubernetes integration below
verifies actual issuance, requested renewal, key rotation and CA replacement
without restarting Trino. Automatic renewal scheduling and expiry/failure alerts
remain separate operational checks.

A token-less init container and sidecar validate key/certificate matching, SAN and
validity. They deploy a combined PEM atomically on a private, memory-backed volume.
Invalid renewal input withholds readiness and preserves the last valid PEM. Trino
reloads updated certificates every ten seconds. Only port 8443 is admitted from the
control plane and controller when TLS is enabled; JWT audience/signature checks are
then enforced. TLS-enabled layers use Trino's native automatic internal certificates
for discovery and exchange, bound to the layer's node secret and node environment.
Only port 8443 is allowed between those Trino pods. The plaintext HTTP listener is
disabled, and the coordinator Service no longer exposes an internal HTTP port.
The existing `mode: disabled` compatibility profile still uses authenticated HTTP.

The coordinator discovers itself over verified loopback HTTPS. Workers resolve the
namespace-local `fdp-trino-discovery` headless Service to the coordinator's
pod IP before starting, since native internal certificates cover IP addresses.
The Service publishes not-ready addresses to avoid an initialization dependency
cycle. A worker liveness check detects when that endpoint no longer contains its
recorded coordinator address and restarts Trino with fresh discovery settings.
Temporary DNS failures do not trigger these restarts. Coordinator replacement can
interrupt active queries; this provides recovery, not uninterrupted failover.
No hostname-verification bypass or extra Kubernetes API access is added. See
[Trino internal TLS](https://trino.io/docs/current/security/internal-communication.html).

New SQL requests snapshot the server-derived engine scheme; result pages and
cancellation keep that scheme. Enabling HTTPS waits for active layer SQL requests,
and the infrastructure API refuses a downgrade back to plaintext. Initial legacy
`tls.mode: disabled` layers keep their current HTTP behavior, whose boundaries
depend on the layer-bound policy context and coordinator ingress restrictions.

`make test-layer-tls` uses a disposable Trino 484 coordinator and two workers with a
fixture policy service to check real JWT rejection, an authenticated distributed join,
native internal TLS, rejection of a wrong node secret or layer identity,
wrong CA/hostname rejection, plaintext HTTP rejection and live leaf renewal. It
uses cached `fdp/control-plane:k3d-test` by default; override
`FDP_NETWORK_TEST_SERVER_IMAGE` with a locally built control-plane image.
This test does not prove RBAC or cert-manager integration. Runtime images deployed
through Helm must be rebuilt to include the new `app.tls_bundle` module.

For the real cert-manager and kubelet path, run:

```sh
make test-certificate-renewal
```

This creates a disposable k3s 1.33.6 cluster with a private kubeconfig, installs the
official cert-manager 1.21.2 manifest pinned by checksum, and uses the generated
Trino deployment. A CA issuer creates the layer's certificate. The test requests
renewal using the same Issuing condition as `cmctl renew`, checks private-key
rotation, then waits for the projected Secret, certificate watcher and Trino reload.
It also replaces the issuer CA with overlapping public trust, verifies that old-only
trust rejects the new issuer, removes the old root, and checks that verified queries
still succeed without restarting either Trino node or its sidecar during renewal.
The fixture uses a worker-backed TPCH query with generated catalog configuration.
A separate recovery phase replaces the disposable coordinator and verifies that
headless discovery restarts the worker after an IP change and restores verified
queries. Renewal is requested manually; this does not shorten or simulate the
automatic expiry timer.

The runner uses `FDP_NETWORK_TEST_SERVER_IMAGE` (default
`fdp/control-plane:k3d-test`) as a cached dependency image, with the current
Python sources mounted as a zip. It downloads the pinned cert-manager manifest and
images if needed. For a predownloaded manifest, set
`FDP_CERT_MANAGER_MANIFEST`; its checksum is still enforced. The manifest pin
is in `tests/kubernetes-integration/cert-manager.lock.json` under the control plane.
The cluster and its temporary volumes are deleted afterward. The test never uses
the active Kubernetes context, live Compose data, or production certificates. Its
allow-all policy fixture verifies certificate transport rather than RBAC.

Production CA rotation should distribute overlapping roots before changing the
issuer, explicitly renew affected leaves, verify clients have switched, and only
then remove the old root. Use the supported
[cmctl renewal command](https://cert-manager.io/docs/reference/cmctl/) rather than
deleting live Secrets. Never export CA private keys into Helm values or Terraform
state. The test's CA is disposable; real issuer backup/recovery and expiry/failure
alerts require separate operational verification.

## Layer-local Java trust

Set `trino.tls.trust_bundle` to a namespace-local Secret key containing only PEM
CA certificates, for example:

```yaml
trino:
  tls:
    mode: existing
    secret_name: trino-server-tls
    trust_bundle:
      name: trino-roots-v1
      key: ca-bundle.pem
```

A non-root, tokenless init container validates the CA certificates and creates
a Java PKCS12 truststore in a memory-backed volume. Every Trino JVM uses it through
the standard JSSE properties. Only the selected public CA key is projected; neither
the controller nor the platform reads that Secret. Invalid, expired, non-CA, or
private-key-containing input prevents startup. This requires a rebuilt control-plane
image with `app.java_trust` and cryptography 45 or newer.

The bundle **replaces the JVM default trust anchors**. Include required public roots
alongside private roots. It does not enable TLS or hostname verification for a
connector automatically: those connector settings remain mandatory. For PostgreSQL,
use `sslmode=verify-full&sslfactory=org.postgresql.ssl.DefaultJavaSSLFactory` in its
JDBC URL to use JSSE trust with hostname verification. Connectors that use a separate
TLS library or explicit connector truststore need their own configuration.

For rotation, create a new versioned Secret with overlapping old/new roots, update
the infrastructure trust reference, and wait for its controlled Trino rollout and
Ready status. Do this **before** switching the issuer of the layer's Trino certificate:
with a trust bundle set, Trino's internal coordinator/worker HTTPS verifies against it too,
so a leaf from a CA it does not yet contain breaks queries (`PAGE_TRANSPORT_ERROR`). Switch the source certificate, verify queries, then repeat with a new
Secret omitting the old root. Updating a Secret in place does not reload the JVM
truststore. The existing retained Secret and previous trust version permit recovery;
never delete the old issuer or trust until all clients have switched. The rollout
uses Recreate and can interrupt active queries; schedule it during a maintenance
window. This does not change the live-reload behavior of Trino's server leaf.

`make test-layer-java-trust` runs the actual non-root init command and Trino 484
against isolated PostgreSQL 16 sources. It verifies an authenticated query with
trusted TLS and rejects both wrong-host and untrusted-CA sources, then removes all
its containers and network. The allow-all policy fixture tests transport, not RBAC.
The implementation uses cryptography's
[Java truststore serializer](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/#cryptography.hazmat.primitives.serialization.pkcs12.serialize_java_truststore);
PostgreSQL TLS settings follow the connector's
[connection security configuration](https://trino.io/docs/current/connector/postgresql.html#connection-security).

## Control-plane health

`/readyz` returns 200 only after initialization and a successful metadata database
query. Failures return 503 with `Initializing` or `MetadataUnavailable`, without
connection details. Helm uses it for startup and readiness; startup allows ten
minutes for initialization. `/healthz` stays independent of external dependencies
and is used for liveness, avoiding restart loops during metadata outages. These
checks do not establish storage, IdP, issuer or external secret-backend readiness;
layer provisioning reports its own detailed prerequisite failures.

## Optional layer data PostgreSQL

Managed layers can provision a separate single-instance PostgreSQL database, with
its own retained PVC. This is independent of `metadataDatabase` and never hosts
Floren Data Platform's operational metadata or Keycloak data. Configure the layer's
infrastructure API, for example:

```yaml
postgres:
  enabled: true
  image: postgres:16.15-bookworm
  cpu: 0.5
  memory: 1Gi
  size: 20Gi
  catalog: warehouse_nonprod   # optional: expose it to this layer's Trino
  tls:
    mode: existing
    secret_name: layer-postgres-tls
```

Without `credentials_secret`, the controller generates the passwords once into
`fdp-postgres-credentials` **inside the layer namespace**; they never leave it
and are never returned by the API. To supply your own, create a Secret in that
namespace with distinct `postgres-password` and `engine-password` keys and name it
in `credentials_secret`. The database pod alone mounts the bootstrap administrator
credential. Initial setup creates database `fdp`, owned by login
`fdp_engine`, which has no superuser, role creation, database creation or
replication privileges. The controller never reads or copies these credentials.

TLS uses standard `tls.key`/`tls.crt` and the exact service SAN
`fdp-postgres.<layer-namespace>.svc`. `mode: cert_manager` also renders a
Certificate using the configured layer issuer. Platform `trust.caBundleSecret`
must trust that issuer: the controller verifies the PostgreSQL SSLRequest/TLS
handshake, including hostname and chain, before reporting the layer Ready.
Plaintext clients are rejected when TLS is enabled. Disabled TLS remains a
compatibility option. Server `trust_bundle`/client-certificate authentication is
not implemented; configure client trust separately. A restricted sidecar uses
the local PostgreSQL socket to reload projected certificate changes. It has no
Kubernetes token or database-password mount. PostgreSQL performs native key/cert
validation during reload. Expiry/failure alerts remain separate work.

Database ingress accepts the same namespace's Trino pods and the explicitly
selected platform control-plane/controller pods. Ordinary apps/workspaces and
other layers cannot connect directly. Trino gets a matching same-layer egress
rule. An internal PostgreSQL integration should use that service hostname and
empty external `egress`; do not add Pod/Service CIDRs to external data grants.

With `catalog` set, the controller writes the Trino catalog file itself (ConfigMap
`fdp-trino-catalogs`, no secret values): it logs in as `fdp_engine` to
database `fdp`, and Trino reads the password from the credentials Secret
through `${ENV:...}`. With TLS it uses
`sslmode=verify-full&sslfactory=org.postgresql.ssl.DefaultJavaSSLFactory`, so the
infrastructure must set `trino.tls.trust_bundle` with the database's CA (refused
otherwise). Register a storage integration whose Trino catalog name matches, in
the same layer, so the platform can grant and govern it. Catalogs from
`trino.catalog_secret` are mounted alongside generated ones; their names must not
collide.

The server runs as UID/GID 999 with a retained ReadWriteOnce PVC. CPU, memory, pod
and PVC/storage consumption are included in the layer budget. Pod replacement
reuses the data directory and does not reinitialize roles/passwords. Updating a
bootstrap Secret in place does **not** rotate an existing database password.
Changing an initialized database's image, credential reference or volume settings,
disabling its storage, and downgrading TLS are refused until an explicit
migration/retirement workflow exists. This deployment is not highly available.

## Optional layer RustFS and Iceberg

A managed layer can also run RustFS (S3-compatible, Apache-2.0) with its own retained
PVC, and combine both stores into Iceberg tables: table metadata in the layer
PostgreSQL (Iceberg JDBC catalog, database `iceberg`), data files in a RustFS bucket.
No other catalog service is needed.

```yaml
postgres: { enabled: true }
rustfs:
  enabled: true
  image: rustfs/rustfs:1.0.2     # pin a tested version
  size: 50Gi
  buckets: [lake]                # created when missing, never deleted
iceberg: { enabled: true, catalog: lake_nonprod, bucket: lake }
```

RustFS keys are generated into `fdp-rustfs-credentials` in the layer namespace
unless `credentials_secret` names your own (keys `access-key`, `secret-key`). The
server reads them from files; a sidecar using the control-plane image creates the
buckets and gates readiness. Only that layer's Trino can reach RustFS; workloads,
apps and other layers cannot. A setup sidecar in the PostgreSQL pod creates the
`iceberg` database and Iceberg's two catalog tables, owned by `fdp_engine`,
for new and existing databases alike. The generated Iceberg catalog disables the
`register_table` and `add_files` procedures and defers access to the platform policy.
Register an `iceberg` storage integration with the same catalog name in that layer.

TLS: set `rustfs.tls` like the PostgreSQL TLS (`existing` or `cert_manager`, SAN
`fdp-rustfs.<layer-namespace>.svc`); it requires `trino.tls.trust_bundle` with the issuing CA,
which Trino's S3 client and the bucket sidecar verify against. RustFS reads its certificate only at
startup, so a sidecar restarts just the RustFS container when a renewed certificate arrives (a short
S3 interruption per renewal; nothing else restarts), and certificate monitoring covers it. TLS cannot
be downgraded to plaintext once enabled.

Limits: one RustFS instance, not highly available; Trino uses the RustFS root keys rather than a
separate engine user. Credentials, storage class and
size cannot change in place; disabling RustFS is refused; buckets may be added.
Retirement scales RustFS and PostgreSQL to zero and keeps their volumes.
`make test-e2e-install` provisions both stores and Iceberg in Non-Prod and Prod
through OpenTofu, writes and reads a table through the platform SQL API, checks the
metadata and data files land in that layer, refuses the other layer's catalog, and
re-reads the table after a Helm upgrade and rollback.

## Catalogs for external data sources

In a managed layer, the platform also writes Trino catalogs for external storage integrations of
that layer, so nobody hand-writes catalog files:

- **PostgreSQL**: give the integration an *engine login* (the read/write service login Trino uses).
  `sslmode` carries over; `verify-ca`/`verify-full` use the layer's Java truststore
  (`trino.tls.trust_bundle`).
- **Iceberg (REST catalog)**: give it *engine storage credentials* (S3 access key) for the table data.

The access-setup (provisioner) login never reaches the engine. The control plane writes the
catalogs into the Secret `fdp-source-catalogs` in that layer's namespace only (it retries
until the namespace exists); the controller never reads it and rolls the layer's Trino when its
digest changes, which briefly interrupts that layer's queries. Integrations without engine
credentials, and catalogs from `trino.catalog_secret`, keep working as before; catalog names must
not collide with those. An integration named after the layer's own PostgreSQL or Iceberg catalog
stays bound to that layer store. Values containing `${` or line breaks are refused (Trino would
substitute or misparse them). Grant the integration's network `egress` as usual; it opens the path
for that layer's Trino only. `make test-e2e-install` registers a PostgreSQL outside the cluster,
queries it through the generated catalog, and checks the catalog stays in its layer and workloads
cannot reach the database.

`make test-layer-postgres` uses a disposable k3s cluster/private kubeconfig with
the generated resources and cached `postgres:16.15-bookworm` plus the configured
control-plane test image. It checks engine privileges and DDL/DML, native TLS and
plaintext/wrong-host/admin rejection, ordinary-execution and cross-layer CNI denial,
live certificate reload without container restarts, and data/login retention after
pod replacement. A Python pod represents the engine's network identity; this does
not claim automatic catalog wiring or full Trino/IAM integration. The cert-manager
resource and readiness aggregation have unit coverage; this test supplies an existing
TLS Secret. All test resources are removed afterward. PostgreSQL's
[TLS setup and key permissions](https://www.postgresql.org/docs/16/ssl-tcp.html)
describe the underlying native TLS behavior.

## Optional metadata PostgreSQL

Set `metadataDatabase.enabled: true` or use `examples/bundled-metadata.yaml`.
The chart creates a persistent PostgreSQL server, a distinct `fdp` database
and non-superuser application role, and connects the control plane automatically.
Supply platform credentials through `existingSecret`, or enable automatic
generation with `bootstrap.enabled`.

Database passwords are generated on initial installation and reused through Helm
lookup on upgrades. The Secret and PVC have Retain behavior. A missing key in an
existing retained Secret fails rendering rather than inventing a replacement
password that would no longer match the stored database. Back up both data and
credentials. Offline `helm template` has no cluster lookup and generates sample
passwords; do not treat repeated offline output as credential rotation instructions.

Alternatively set `metadataDatabase.existingSecret` to a Secret containing
`postgres-password` and `fdp-password` (plus `keycloak-password` when sharing
the server with bundled Keycloak). The application receives only the
app password. The database bootstrap Job uses the administrator password, creates
the app role/database idempotently, and runs again on release upgrades. It does
not silently change an existing role's password. Coordinate database password
changes and Secret updates explicitly until rotation automation is implemented.

The database and PVC are single-instance and use the cluster's default storage
class unless configured otherwise. This is not an HA or automated-backup profile.
By default only the control plane, bootstrap Job, optional Keycloak and administrative metadata
Trino can reach the database through the chart's ingress policy. Enabling Airflow sharing adds
the explicit Airflow component access described below. Its administrator
credentials are never sent to workload namespaces.

For external PostgreSQL, leave `metadataDatabase.enabled: false` and set
`FDP_DATABASE_URL` in the platform Secret as before. Split settings
`FDP_DATABASE_HOST/PORT/NAME/USER/PASSWORD` are also supported; setting HOST
opts into that form and overrides DATABASE_URL, safely escaping the password.
Alternatively `metadataDatabase.connectionSecret` references a namespace-local
Secret with a `database-url` key. Bundled metadata uses an ordinary database-owner
role, so automatic `metadataSql.bootstrapReader` is rejected: provision the reader
separately until the dedicated reader-bootstrap integration is implemented.
When Keycloak shares the server, both databases revoke PUBLIC connection access
and grant it explicitly to their own application roles. The two application
credentials cannot connect to each other's database. Provision an administrative
metadata reader with an explicit CONNECT grant as well as SELECT privileges.

## Bundled and external profiles

`examples/bundled.yaml` enables metadata PostgreSQL, generated platform keys,
Keycloak, SCIM synchronization, and the bundled OpenBao secret store.
`examples/external-services.yaml` keeps external PostgreSQL, OIDC/SCIM, and OpenBao.
Keycloak can also use its own external database independently of metadata PostgreSQL:
set `keycloak.database.existingSecret` with `jdbc-url`, `username`, and `password`.

Configure real public DNS names, the existing RWX/NFS export and workspace storage
class, compatible server/runtime images, and public TLS. The example NFS host is a
placeholder, not a provisioned storage server. Fully layer-local artifacts and
automatic isolated namespaces are later steps. This profile is not yet a complete
fresh-cluster deployment or verified upgrade path.

```sh
helm install fdp deploy/helm/fdp -n fdp --create-namespace \
  -f deploy/helm/fdp/examples/bundled.yaml \
  -f deploy/helm/fdp/examples/tls-cert-manager.yaml \
  -f my-installation-values.yaml --wait --timeout 15m
```

Automatic bootstrap generates independent session, gateway, Trino-signing, and SCIM
keys. Its Trino HMAC file is derived from the matching platform key. Existing keys
are reused through lookup, missing retained keys fail rendering, and credentials
are retained on uninstall. Bootstrap does not rotate keys or migrate existing
secret backends. Helm release history contains rendered Secrets: protect access
to that history, backups, and any Terraform state containing rendered manifests.

The gateway uses only HTTP/file providers; its unnecessary upstream cluster-wide
Secret-reading RBAC is disabled. A custom `existingSecret` must also be referenced
by `traefik.env` for `FDP_GATEWAY_CONFIG_TOKEN`; validation rejects mismatches.

## Keycloak and directory provisioning

Bundled Keycloak uses a dedicated database/user and a separately retained Secret
containing `admin-password`, `initial-user-password`, and `provisioner-client-secret`.
Provide `keycloak.existingSecret` to manage those credentials externally. The realm
import creates a PKCE desktop client, an initial app administrator, an IdP admin
group, and a read-only directory provisioning client. Password grant is disabled
by default. The initial app password is temporary and must be changed at first login.

`keycloak.publicUrl` must use a separate HTTPS hostname from the app and API.
The platform gateway routes that host to Keycloak; its management port is never
publicly exposed. The chart derives issuer/JWKS configuration and certificate SANs.

The single-replica provisioning adapter runs without a Kubernetes API token. It
receives only its read-only IdP client secret and the SCIM bearer, not IdP admin,
database, signing, or application user passwords. It provisions users, effective
realm roles, and nested groups through Floren Data Platform's standard SCIM API. It preserves
stable IDs, skips unchanged records, propagates disabled users, and retires only
its own realm-scoped external IDs after reading a complete snapshot. Empty,
truncated, repeated, or failed inventories do not retire identities.
The SCIM bearer is directory-authoritative; protect it as a privileged provisioning
credential even though it is not a general control-plane administrator token.

The adapter's readiness probe reports whether synchronization is succeeding. The
default interval is 60 seconds, configurable under `keycloak.directorySync`.
Keep `oidc.directoryMode: scim`; other IdPs use their own SCIM provisioning.
IdP service-account users are excluded from general directory import. Future
Terraform automation principals need an explicit, scoped provisioning/bootstrap
path; do not reuse the directory reader or give execution identities admin rights.

Realm import initializes a missing realm and does not continuously reconcile an
existing realm. Changing imported clients, passwords, realm name, or issuer after
installation requires an explicit IdP update/migration. Helm rollback alone cannot
roll back IdP state or database writes.

## Secret stores

`secretManagement.backend` selects where application secrets (data-source, Git and API credentials,
certificates, per-deployment Airflow keys) are stored. Values are write-only through the API and never
enter metadata tables. Changing backends requires a coordinated secret migration.

| Backend | Configure | Platform identity |
|---|---|---|
| `openbao` bundled | `openbao.enabled: true` | Kubernetes auth, configured automatically |
| `openbao` / `vault` external | `openbao.addr`, `openbao.kvMount`, `openbao.auth.*` | Token, Kubernetes auth or AppRole |
| `kubernetes` | `secretManagement.namespace` | The control plane's service account |
| `aws` | `secretManagement.aws.*` | IRSA or EKS Pod Identity (`serviceAccount.annotations`) |
| `azure` | `secretManagement.azure.vaultUrl` | Azure Workload Identity (`serviceAccount.annotations`, `podLabels`) |
| `gcp` | `secretManagement.gcp.project` | GKE Workload Identity (`serviceAccount.annotations`) |

Dynamic database credentials (`dynamic_role`) need the OpenBao or Vault backend.

### Bundled OpenBao

`openbao.enabled: true` runs OpenBao (MPL-2.0) as a StatefulSet with Raft storage in its own namespace
(`<release-namespace>-openbao`, Pod Security `restricted`, default-deny network policies; only the
control plane may connect). On its first start, pod 0 initializes itself declaratively: a KV v2 mount,
Kubernetes auth with a role bound to the `fdp-control-plane` service account, and a policy limited to
that mount. OpenBao revokes the root token used for this immediately; the chart never stores a root
token or unseal key. Every request is audited, with secrets HMAC-hashed, on the pod log.

Two unseal models, both automatic:

- `openbao.seal.mode: kubernetes` (default): a static 32-byte seal key in the Secret `fdp-openbao-seal`.
  An init container creates it once as an immutable Secret and never rewrites it, so GitOps renders
  without cluster lookups cannot replace it. Anyone who can read that Secret and the OpenBao volume
  can read the stored secrets: enable etcd encryption at rest and keep a protected copy of the key.
  To bring your own key, set `openbao.seal.existingSecret` (key `key`, exactly 32 bytes).
- `openbao.seal.mode: kms`: auto-unseal through your KMS (`awskms`, `azurekeyvault`, `gcpckms`,
  `transit`, ...). `openbao.seal.kms.config` is passed to the OpenBao seal stanza; give the OpenBao pods
  access with `openbao.serviceAccount.annotations`, `openbao.podLabels` or `openbao.extraEnv`.
  No key is stored in the cluster.

`openbao.replicas: 3` (or 5) runs a Raft cluster. Pods start in order: pod 0 initializes and the others
join it. After installation you may set `openbao.selfInitialize: false`; with it enabled, never delete
pod 0's volume while other members still run. `openbao.tls.mode` (`existing` or `certManager` with a
private-CA issuer) encrypts client traffic; renewed certificates are reloaded without a restart. Add
the CA to `trust.caBundleSecret` so the control plane verifies it. With a private registry, create the
pull Secret in the OpenBao namespace too (the seal-key init container uses the control-plane image).

Administration uses a break-glass role: whoever may create tokens for the `fdp-openbao-admin` service
account (`kubectl create token`) can log in with Kubernetes auth (`role=admin`) for at most an hour. No pod
runs as that account. Back up the Raft data with `bao operator raft snapshot save` and, in `kubernetes`
mode, the seal key Secret: without the key, the data cannot be decrypted. See
[operations](../../../docs/kubernetes-operations.md#secret-backend-recovery-openbao).

### Kubernetes Secrets

`secretManagement.backend: kubernetes` stores application secrets and certificate
bundles in a dedicated namespace, defaulting to `<release-namespace>-secrets`.
Only the control-plane service account gets Secret get/create/update/delete there;
workloads and the directory adapter receive no such permissions. App IAM still
authorizes each access. Secret names are hashes of internal paths. The namespace
is retained to preserve stored secrets. Kubernetes base64 encoding is not
encryption: configure cluster encryption at rest and encrypted backups.

### Cloud secret managers

Each platform secret becomes one provider secret holding a JSON map, named with the configured prefix
plus a readable form of its path and a hash. The control plane needs permission to create, read, update
and delete secrets with that prefix (AWS also `RestoreSecret`; Azure `recover`; Google
`secretmanager.secrets.*` and `versions.*`). AWS keeps deleted secrets for
`secretManagement.aws.recoveryWindowDays`; Azure soft-deletes; Google keeps only the latest version.

References: [Kubernetes Secret guidance](https://kubernetes.io/docs/concepts/security/secrets-good-practices/),
[Keycloak realm import](https://www.keycloak.org/server/importExport),
[Keycloak reverse proxy](https://www.keycloak.org/server/reverseproxy),
[certificate lifecycle](https://cert-manager.io/docs/usage/certificate/).

## Local rendering checks

```sh
helm lint deploy/helm/fdp
helm template fdp deploy/helm/fdp -n fdp \
  -f deploy/helm/fdp/examples/tls-cert-manager.yaml
```

Use the Python chart regression tests for semantic checks of the rendered TLS
configuration, certificate resource, private-key mounts, and disabled/external modes.
These commands do not deploy to a Kubernetes cluster.

An isolated Docker integration check verifies the rendered database bootstrap,
Keycloak realm/credential import, read-only provisioning client, SCIM nested groups,
idempotency, real OIDC admin sign-in, and disabled-user propagation:

```sh
services/control-plane/.venv/bin/python \
  services/control-plane/tests/kubernetes-integration/verify_bootstrap.py
```

It creates temporary containers/network and an in-memory control-plane database,
then removes those test resources. It does not access the current Kubernetes
context or live Compose data. First-login enrollment is simulated in this test;
password grant is enabled only in its disposable rendered realm.

## Shared operational metadata namespace

Set `metadataDatabase.namespace: fdp-system-data` and
`metadataDatabase.airflow.enabled: true` to use one shared PostgreSQL instance for FDP,
optional Keycloak, and a separate private database per Airflow publication. Standalone
and distributed deployments retain their own layer placement. No per-publication
PostgreSQL pod is needed. See [configuration, isolation and migration](../../../docs/shared-system-data.md).
Changing namespaces on an existing installation requires backup/restore and a verified
cutover; Helm cannot move an existing PVC. The database namespace and persistence are
retained on uninstall. The old release-local database placement remains the default
for compatibility until explicitly configured.
