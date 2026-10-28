# Shared operational database service

FDP may place its optional bundled PostgreSQL in a dedicated namespace, separate from
both the control plane and user-created layers. This stores operational metadata;
business datasets continue to use the storage integrations of their own layer.

```yaml
metadataDatabase:
  enabled: true
  namespace: fdp-system-data
  airflow:
    enabled: true
```

The Floren installation includes these settings in
`deploy/environments/floren/system-data.yaml`; `install.sh` includes that overlay.
An empty namespace keeps the old release-local placement for compatibility.

## Database and credential boundaries

One PostgreSQL server contains `fdp`, optional `keycloak`, and `airflow_<publication-id>`
for each Airflow publication. Every Airflow publication has its own stable login,
Fernet key and webserver secret in the configured FDP secret backend. Test and Prod
publications receive independent databases and credentials, even with identical code.

Only the control plane receives the trusted database provisioner's credentials. Its
login may create databases and roles; it is not a PostgreSQL superuser. Tenant roles
cannot create roles/databases, replicate or bypass RLS. PUBLIC database privileges
are revoked by the actual database owner. Provisioning verifies existing ownership,
is serialized across API replicas, retains credentials and caps each login at 32
connections. Each Airflow SQLAlchemy pool is limited to two connections plus one
overflow connection per process. Larger distributed deployments may need a configured
connection pooler and adjusted limits; shared service capacity remains finite.

Both standalone and distributed Airflow use this server when configured. Standalone
uses LocalExecutor with two orchestration task slots by default, configured through
`airflow_settings.local_parallelism`. The normal scheduler, webserver and triggerer
run together and restart together on failure; the development-only `airflow standalone`
command is reserved for the local SQLite fallback. Tasks still submit actual compute
work to FDP warehouses.
Distributed deployments retain their layer-local authenticated broker and workers,
without a PostgreSQL pod per deployment. No runtime receives the FDP, Keycloak or
provisioner database password. FDP reads recent orchestration history with the
publication's database login, read-only transactions and bounded query timeouts.

The dedicated namespace has default-deny ingress and egress. Ingress permits the
specified control-plane services and Airflow components from namespaces belonging to
this installation. Layer egress permits only Airflow standalone/scheduler/web/worker
pods to reach this database service on TCP 5432. Workspaces, regular jobs and apps do
not receive that exception. PostgreSQL permissions enforce isolation between databases
on the shared endpoint. Namespace isolation does not provide separate failure or
resource boundaries; this is an explicit shared infrastructure dependency.

The database namespace, PVC and generated Secrets are retained on Helm uninstall.
External credentials supplied using `metadataDatabase.existingSecret` must exist in
both the control-plane and database namespaces when those differ. Never put passwords
in Helm values or OpenTofu state. Provisioner password changes require coordinated role
and Secret updates; bootstrap never silently changes existing credentials.

For external PostgreSQL, configure `FDP_AIRFLOW_METADATA_URL` (trusted provisioner URL)
in the platform Secret. Existing TLS URL parameters are preserved in the per-publication
connection. Certificate private keys stay in Secrets, and CA trust/certificate files
must be mounted for the clients that reference them. The bundled server currently uses
internal TCP with CNI policy isolation; server TLS and automated backup/HA are separate
configuration work and must not be assumed from the namespace change.

## Existing-installation migration

Changing a namespace does not move a PVC. Do not perform an ordinary Helm upgrade
against an existing database until its state has been migrated and verified.

1. Save the current Helm values and release revision in protected operator storage.
2. Render with server-side lookups so retained credentials are reused. Prepare the new
   namespace, database Secret, PVC, Service, network policies and PostgreSQL StatefulSet.
   Keep the old database running. Do not expose a new, empty database to FDP.
3. Stop metadata writers: FDP control-plane replicas, layer controller, directory sync
   and Keycloak. Confirm no active pipeline jobs. Keep Airflow DAGs paused.
4. Dump PostgreSQL roles and each operational database using PostgreSQL tools matching
   the server version. Store backups with mode 0600 outside Git. Restore roles and
   databases into the new server; do not copy or recreate application passwords.
5. Compare every application's table row counts and content hashes. Run the shared
   provisioner bootstrap only after restoring the original roles/databases.
   For the Floren installer, record verification with `kubectl -n fdp-system-data create
   configmap fdp-migration-verified --from-literal=status=verified` only after this comparison.
   The installer refuses an ordinary namespace-changing upgrade while the old server exists
   without this verification marker.
6. Upgrade Helm using the new namespace and tested control-plane image while preserving
   pinned runtime images and all other installation values. Restore intended replica
   counts and verify FDP readiness, Keycloak login and layer reconciliation.
7. Redeploy each Airflow publication through FDP. Existing nonempty orchestration history
   blocks automatic backend switching; explicitly migrate it first. Empty deployments,
   including the freshly reset Pokémon deployments, can initialize PostgreSQL directly.
   Verify paused DAG state, history, private database credentials and cross-database denial.
   For a matching Airflow runtime, `deploy/airflow/migrate_metadata.py` copies an offline
   SQLite snapshot into an initialized empty PostgreSQL database. It verifies schema
   revisions and every copied row, repairs sequences, and refuses nonempty targets.
   Run it only with old writers stopped, preserve the old Fernet key, and keep its
   PostgreSQL connection in `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` rather than argv.
8. Keep the old PVC and protected backups for rollback. Once the new server accepts
   writes, rollback requires synchronizing those writes; reverting Helm alone is unsafe.
   Remove an unused old database server, but do not automatically delete rollback storage.

Current deployment progress and verification evidence are recorded in
`docs/shared-system-data-progress.md`. No OpenTofu business-object changes are required:
Helm configures shared infrastructure and FDP automatically provisions each publication's
operational database. Existing layer/workload OpenTofu definitions remain authoritative.
