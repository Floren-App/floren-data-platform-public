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

An empty `namespace` places the database in the release namespace. You can use your own
PostgreSQL instead: see [external PostgreSQL](#external-postgresql).

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

## External PostgreSQL

For external PostgreSQL, configure `FDP_AIRFLOW_METADATA_URL` (trusted provisioner URL)
in the platform Secret. Existing TLS URL parameters are preserved in the per-publication
connection. Certificate private keys stay in Secrets, and CA trust/certificate files
must be mounted for the clients that reference them. The bundled server currently uses
internal TCP with CNI policy isolation; server TLS and automated backup/HA are separate
configuration work and must not be assumed from the namespace change.

## Moving an existing database

Changing `metadataDatabase.namespace` does not move a PersistentVolumeClaim. To move an
existing bundled database, or to switch to your own PostgreSQL:

1. Stop metadata writers: the control plane, layer controller, directory sync and
   Keycloak, if bundled. Pause Airflow DAGs and let active jobs finish.
2. Dump the roles and every operational database with PostgreSQL tools matching the
   server version. Keep the backups outside Git, readable only by operators.
3. Restore roles and databases into the new server without recreating passwords, and
   compare row counts and content of every database.
4. Point the installation at the new server, upgrade, and verify readiness, sign-in,
   layer reconciliation and Airflow history.
5. Keep the old volume and backups until the new server has run successfully. Once it
   accepts writes, rolling back requires synchronizing those writes. Reverting Helm alone
   is not enough.

For a matching Airflow runtime, `deploy/airflow/migrate_metadata.py` copies an offline
SQLite Airflow database into an initialized, empty PostgreSQL database. It verifies schema
revisions and every copied row, repairs sequences and refuses nonempty targets. Run it with
the old writers stopped, keep the original Fernet key, and pass the PostgreSQL connection
through `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` rather than the command line.

Helm configures the shared database service. FDP provisions each Airflow publication's
database automatically, so no OpenTofu changes are needed.
