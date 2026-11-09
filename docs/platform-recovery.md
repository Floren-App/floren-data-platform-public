# Backup plans and recovery

Backup plans select **what** to protect. Reusable destinations select **where** an
archive goes. Plans have independent paths, schedules, timezones, retention and
execution histories. Two plans may protect the same source to different locations.
Nothing is scheduled until a plan is explicitly enabled.

## What is protected

| Source | Method and boundary |
|---|---|
| Control plane | Dedicated FDP PostgreSQL metadata: RBAC, catalog, requests/cases, configuration and run history. The archive contains vault references, not secret values. |
| PostgreSQL integration | Consistent native `pg_dump`, for the whole registered database or selected schemas. Requires matching PostgreSQL client/server major versions and sufficient database backup permissions. |
| S3 files integration | Current objects under the integration prefix or a selected child prefix. Conditional reads detect changes to an enumerated object. Each object records its original key, version ID when available, size and SHA-256. |
| Iceberg integration | Iceberg v1/v2 tables: captured catalog pointers, table metadata, every retained snapshot’s manifest lists/manifests, live data/delete files, statistics and partition statistics. External REST and platform-managed JDBC catalogs are supported. |
| Other integrations | Require their native backup adapter; unsupported plans are rejected. |

S3 export is a collection of verified per-object reads, **not a cross-object atomic
snapshot**. Historical versions and delete markers are not exported. Use the
storage provider's native versioning/replication/snapshot capabilities when those
are required. Schema-selected PostgreSQL dumps may need dependencies outside the
selected schemas during recovery; whole-database backup is the default.

Workspace volumes, artifacts, uploaded packages, Kafka messages, Airflow databases,
the identity provider, Git, Terraform state and the selected vault require their
own native recovery arrangements. Control-plane backup does not silently include
them. Containers and caches are recreated.

## Secrets stay in the selected vault

FDP uses the installation's **existing configured SecretStore**: OpenBao/Vault,
Kubernetes Secrets, AWS Secrets Manager, Azure Key Vault or Google Secret Manager.
There is no separate backup secret store.

- Backup destinations store a `recovery_key_ref`, never a private key or password.
- The referenced secret needs a `public_key` field containing an RSA public key
  of at least 3072 bits. The backup worker needs no private key. Keep `private_key`
  and optional `password` in a separately authorized secret used for recovery.
- Platform signing keys and internal tokens are versioned **inside that same
  selected vault** at `platform/recovery/runtime/<version>`. A control-plane
  archive contains the reference, not a `platform-keys.json` secret bundle.
- Source/destination credentials are read from their existing integration secret
  references when needed. They are not placed in plans, manifests or Terraform state.
- Configure `FDP_RUNTIME_KEYS_REF=platform/runtime` (or your chosen vault path) to
  load runtime signing keys directly from the selected backend. When this is set,
  missing/invalid vault values fail closed; environment values do not replace them.
  The secret must contain a strong `session_signing_key`; optional key/token fields
  use their disabled defaults when absent. Bootstrap vault authentication continues
  to use the installation's existing workload identity/injection mechanism.

The backup worker's existing vault identity needs read access to its destination's
public-key reference and integration credentials, plus write access to
`platform/recovery/runtime/*` for control-plane key snapshots. Give the recovery
operator a separate vault policy: read the recovery private-key secret and the
referenced runtime snapshot, and write the chosen runtime key reference. Retain
those vault snapshots for at least as long as their archives; archive retention
does not delete vault secrets.

Vault recovery is a prerequisite for restoring FDP. Protect the vault and its
versioned key references using that provider's native backup/versioning tools.
FDP backups never scrape or export the vault. Legacy version-1 archives can still
be verified; recovery imports their embedded keys into the configured vault in
memory and does not extract a secret file. Existing archives are not rewritten.

## Destinations and paths

A destination references an existing S3 file integration, or uses an explicit
bucket/HTTPS endpoint with the installation's workload identity. Integration-backed
credentials use scoped STS sessions; managed RustFS integration credentials use the
same existing backend and require a secure destination endpoint. Credentials stay
out of configuration responses.

The final object path is:

```text
s3://<bucket>/<integration-base>/<destination-prefix>/<plan-prefix>/<run-id>/metadata.fdp
s3://<bucket>/<integration-base>/<destination-prefix>/<plan-prefix>/<run-id>/storage.fdp
```

Prefixes are relative and reject traversal or policy wildcards. A source's backup
cannot be written inside that source prefix: this prevents recursive inclusion.
Object IDs separate runs, and retention operates on the plan's own inventory,
including when several plans share a destination prefix.

Client-side authenticated encryption is mandatory. Each archive uses a fresh
AES-256-GCM key wrapped with the recovery RSA public key. Optional SSE-S3 or SSE-KMS
and S3 COMPLIANCE Object Lock can add protection. The destination bucket and its
lifecycle/Object Lock configuration must already exist; FDP does not create them
or weaken retention locks. An integration-backed SSE-KMS destination requires a
full KMS key ARN.

## Configure with Terraform

The key reference below identifies an existing secret in the chosen vault. Do not
put private key material into this configuration.

```hcl
resource "fdp_backup_destination" "offsite" {
  name             = "Offsite recovery"
  bucket           = "company-recovery"
  region           = "eu-west-1"
  prefix           = "production"
  recovery_key_ref = "backups/keys/production-public"
}

resource "fdp_backup_plan" "control_plane" {
  name           = "Nightly platform"
  source_kind    = "control_plane"
  destination_id = fdp_backup_destination.offsite.id
  prefix         = "platform/nightly"
  schedule       = "30 2 * * *"
  timezone       = "Europe/Amsterdam"
  retention_days = 30
  enabled        = false
}

resource "fdp_backup_plan" "orders" {
  name           = "Nightly orders"
  source_kind    = "storage"
  source_id      = fdp_data_source.orders.id
  schemas        = ["public"]
  destination_id = fdp_backup_destination.offsite.id
  prefix         = "orders/nightly"
  schedule       = "0 3 * * *"
  timezone       = "Europe/Amsterdam"
  retention_days = 90
  enabled        = false
}
```

For S3, use `source_prefix` instead of `schemas`. Empty selection backs up the
whole registered database/integration prefix. Create another destination and plan
for a second independent copy. `max_bytes` defaults to 10 GiB and `max_objects` to
100,000, bounding storage exports; failures never report an incomplete archive as
successful. Execution has a bounded window and source credential leases may impose
shorter limits. Large sources may require native provider backup tooling.

The old singleton `fdp_backup_configuration` API/resource remains available for
existing control-plane-only configurations. It uses the new vault-reference
archive format. It is not converted or enabled automatically; independent plans
are the preferred model for new configuration.

## Scheduling, retention and notifications

Choose daily or weekly at a time in the app, or under **Advanced** any five-field cron expression, with an IANA
timezone. `30 3 * * 0` means Sunday at 03:30 in the selected timezone. An advanced schedule that is not daily or
weekly stays advanced; the app never rewrites it. The form previews the next runs, computed by the server exactly as
the scheduler will, and shows the full path the archives go to. **Largest archive** and **Most objects** stop a backup
that would be bigger than expected (10 GiB and 100,000 objects unless you change them). Timezone-aware scheduling accounts for daylight
saving changes; use a time outside the DST transition window for predictable daily
operation. Missed occurrences are coalesced after downtime, not replayed as a backlog.
A plan never overlaps itself; other plans keep their independent queue/history.
Each history row keeps the original source and destination. Moving a plan across
layers requires creating a new plan so existing history retains its scope.

`retention_days = 0` keeps archives indefinitely. After a new backup is uploaded
and its ciphertext is read back and verified, expired successful archives of that
plan can be deleted. The newest verified archive is retained. Object Lock or IAM
failures are audited and notified rather than concealed. Versioned objects are
deleted by the recorded version ID; bucket lifecycle still governs other versions
or external copies. Removing a plan pauses its future work and retains archives
and history. A destination cannot be removed while a plan references it.

Configure existing notification channels for `backup.failed`, `backup.succeeded`
and `backup.retention.failed`. Notifications contain status and plan IDs, not
credentials, database contents, object data or encryption keys.

## Status and readiness

With no plan, the page says **Not configured**: nothing is backed up by a plan, and nothing is turned on for you.
Each plan shows what it is doing (paused, scheduled, running, or last backup failed with the reason) and when it
last completed. **What is protected** lists each source with its enabled plans and its latest completed backup.

**Check prerequisites** in the plan form checks, with the same adapters a backup uses, that you may export the
source and it has a backup adapter, that the referenced vault secret holds a usable recovery public key, and that you
may use the destination, its location is valid and outside the source, and retention fits its Object Lock. It
changes nothing: no bucket policy, retention, encryption, credentials or schedule.

A completed backup is an archive whose checksum was verified after upload. That is not evidence that a restore
works: rehearse restores in an isolated environment with the procedures below, and record the results.

## Access control

Viewer on **Backup and recovery** is required for the page. Named permissions
separately control plan reads, creation, metadata updates, schedule changes,
source changes, destination changes, retention changes, manual execution and
archive deletion. They are visible under Access → Groups.

Storage execution requires an explicit `backups.sources.export` grant on the
integration and `backups.destinations.use` on the selected destination. Raw export
delegation requires Global Admin approval; ordinary SQL/file Viewer grants or
masked product access do not grant raw-backup authority. Control-plane backups and
destination registration require active Global Admin. Retention deletion requires
`backups.archives.delete`. Execution rechecks current permissions and the frozen
source/destination before doing work, and refuses changed integration settings.
Schedules use the configuring identity and selected group; revoked authority stops
future execution. Administrative metadata is not access to backup plaintext.

## Verify or extract a storage archive

Get the checksum from trusted inventory/audit evidence and the archive from the
configured destination. The configured vault must contain the private recovery
key under a separately authorized path.

```sh
python -m app.storage_recovery \
  --archive storage.fdp \
  --expected-sha256 TRUSTED_SHA256 \
  --recovery-key-ref backups/keys/recovery-operator \
  --extract-to ./verified-storage
```

Omit `--extract-to` to verify only. The extractor validates the manifest, member
names, hashes and object sizes and refuses an existing output directory. It never
writes back to the original integration. PostgreSQL output is a native
`database.dump`; use `pg_restore --exit-on-error --single-transaction` into a reviewed,
empty target with the matching PostgreSQL client. For S3, `manifest.json` maps each
hashed payload filename to its exact original key, content type and recorded
version. Use your native S3 restore tooling to write those verified payloads to an
explicitly selected recovery bucket/prefix. Do not treat archive filenames as
original S3 paths or mirror deletions into a live bucket. Reconcile native owners,
permissions, extensions and source configuration before reconnecting workloads.

## Recover the control plane

Use a dedicated, empty PostgreSQL target with matching major-version tools. The
worker refuses a metadata database containing unrelated business relations.
Recover the selected vault, IdP and other external dependencies first.

```sh
python -m app.recovery verify \
  --archive metadata.fdp \
  --expected-sha256 TRUSTED_SHA256 \
  --recovery-key-ref backups/keys/recovery-operator

# Supply the target URL through the installation's existing secret injection.
python -m app.recovery restore \
  --archive metadata.fdp \
  --expected-sha256 TRUSTED_SHA256 \
  --recovery-key-ref backups/keys/recovery-operator \
  --confirm-empty-target
```

`FDP_RESTORE_DATABASE_URL` names the empty recovery database. Restore reconciles
runtime keys inside the selected vault at the configured `FDP_RUNTIME_KEYS_REF`
or the recorded runtime reference, defaulting to `platform/runtime`. No key output
file is produced. A committed database guard blocks startup if restore crashes
before finalization; an incomplete target cannot be approved through the API.

Restored workspaces, workloads and in-flight runs are stopped. Restored execution,
Kafka and SQL admissions are invalidated, and SIEM starts a new recovery epoch.
Recovered API replicas/controllers remain quarantined until identities, permissions,
expired approvals, vault secrets and external data dependencies have been reconciled.
Only an active Global Admin can approve those checks. Restart **all** API/controller
replicas after approval before admitting work.

Customer-native PostgreSQL backup/PITR is also supported. Run the offline
`python -m app.recovery quarantine --confirm-restored-target` procedure against the
restored metadata target before starting FDP. Native database recovery does not
recover vault contents or replace native backups of other dependencies.

## Who protects what

| Component | Protected by | Notes |
|---|---|---|
| Control plane metadata | A Limoon backup plan (control plane) | Contains vault references, not secret values |
| PostgreSQL, S3 files and Iceberg integrations | Limoon backup plans for those integrations | Within the boundaries above |
| Metadata database service | You, or the bundled single-instance PostgreSQL | The bundled one is not highly available |
| Secret store (vault) | You, through the vault's own backup | Limoon never exports the vault |
| Identity provider | You, or the bundled single-instance Keycloak | |
| Workspace volumes, packages, artifacts | You, with storage snapshots | Not in Limoon's backups |
| Kafka messages | You, with Kafka's replication or mirroring | |
| Git repositories and Terraform state | You, where they are hosted | |

**Installation readiness** under Administration (for an active Global Admin) shows the same split for your
installation: what is configured, what needs attention, what is not configured, what is single-instance, what you
operate, and what is unknown. It is read-only and claims no protection it cannot see; a restore drill, for example,
stays unknown until you record one.

## Restore drills

A completed backup proves an archive was written and its checksum verified, not that a restore works. Rehearse one
regularly, never over the live installation:

1. Create an isolated environment: its own namespace or cluster, its own database, no route from users, and no
   network path to the live layers.
2. Restore the chosen archive there with the procedure above, using a private key read from the vault for this
   drill only. Do not export or copy the vault.
3. Keep the restored system quarantined: start it without schedulers, workloads or notifications, as for a real
   recovery, and check it before any reconciliation.
4. Check what matters: sign-in, groups and roles, the catalog, a sample of workloads and data access, and that the
   restored release and metadata schema match.
5. Record the evidence, then remove the environment.

Keep a record per drill with no secret values or data in it:

| Field | Example |
|---|---|
| Date and who ran it | 2026-11-07, platform team |
| Release and metadata schema of the archive | abc1234, 0009_management_provenance |
| Plan and archive | Nightly control plane, the archive's object key and SHA-256 |
| Environment | Isolated namespace `drill-2026-11`, no user route |
| Observed RPO | Archive age when the drill began, e.g. 9 hours |
| Observed RTO | From start to a usable restored system, e.g. 47 minutes |
| Result and checks | Passed: sign-in, roles, catalog, three workloads, one data query |
| Follow-ups | What to fix before the next drill |

Report the RPO and RTO you observed, not targets you have not measured.

## Iceberg snapshot backups and recovery

Choose an Iceberg integration in **What to back up**, or use a storage-source
`fdp_backup_plan`. `schemas` selects exact Iceberg namespaces (dot-separated for
nested namespaces); an empty list captures all discovered namespaces and tables.
File-prefix selection is not used for Iceberg. All files must be under the registered
S3 warehouse bucket/prefix; a table with dependencies outside it is rejected.

```hcl
resource "fdp_backup_plan" "lake" {
  name           = "Nightly lake snapshots"
  source_kind    = "storage"
  source_id      = fdp_data_source.lake.id
  schemas        = ["reporting"]
  destination_id = fdp_backup_destination.offsite.id
  prefix         = "iceberg/nightly"
  schedule       = "0 3 * * *"
  timezone       = "Europe/Amsterdam"
  retention_days = 90
  enabled        = false
}
```

Each table is captured from one metadata-file pointer. The archive includes the
files needed by **all snapshots retained in that metadata**, preserving branches,
tags, schema/partition history and empty tables. It includes position/equality
delete files referenced by live manifest entries; deleted entries are not copied.
Objects shared between snapshots are copied once. Capture is consistent per table;
it does not claim a single transaction across every table in a catalog.

Concurrent writes can continue. Concurrent snapshot expiration or orphan cleanup
may remove a required file; the backup then fails instead of publishing an incomplete
success. Schedule maintenance separately or protect the required snapshots through
your catalog while a large export runs. FDP does not modify tags or catalog policy
to acquire backup authority. Expired snapshots, older metadata JSON files referenced
only in `metadata-log`, catalog views and Iceberg v3 tables are outside this adapter.
The archive preserves the original metadata JSON; its historical metadata-log
locations may therefore be unavailable after recovery.

External REST catalogs use `/v1/config`, namespace/table discovery and captured
`metadata-location` pointers. Server-returned configuration and vended credentials
are never archived. If needed, configure the optional **Backup storage/catalog
login** on the existing integration: read-only warehouse `access_key_id` and
`secret_access_key`, optional `session_token`, and optional `catalog_token` for REST
authentication. These use the existing `datasources/<name>/provisioner` secret in the
selected vault. Without a separate backup login the existing engine storage login
is used. Managed JDBC catalogs use the layer's existing credentials and endpoints;
no credentials are copied into the archive.

To recover, first decrypt and verify into a new local directory:

```sh
python -m app.storage_recovery \
  --archive storage.fdp \
  --expected-sha256 '<recorded encrypted archive checksum>' \
  --recovery-key-ref backups/keys/recovery-operator \
  --extract-to /recovery/verified-iceberg
```

Then use an existing recovery integration with the **same warehouse URI** and
credentials authorized to write the recovery storage and register tables:

```sh
python -m app.iceberg_recovery \
  --verified-directory /recovery/verified-iceberg \
  --target-source-id '<recovery Iceberg integration ID>'
```

This is an offline operator command; it uses the platform metadata database and the
installation's selected vault. The target may use a replacement S3 endpoint with
the original bucket/key layout and a new empty REST or managed JDBC catalog.
Arbitrary bucket/path relocation is not supported because Iceberg manifests and
position-delete files may contain absolute data-file references.

Recovery verifies local checksums and traverses the captured dependency graph
before contacting the target. It checks existing catalog entries and objects before
writing. Identical objects can be reused; conflicting bytes or table pointers are
refused. New objects use conditional writes, including conditional multipart
completion, and are read back for verification before catalog registration. The
S3-compatible target must support those conditional operations. Restore is idempotent,
not an atomic transaction across storage and catalog: a failure may leave verified
objects or some registered tables for a subsequent retry. Keep target writers and
maintenance stopped until recovery and query validation finish.

See the [Iceberg table specification](https://iceberg.apache.org/spec/) and
[REST catalog specification](https://github.com/apache/iceberg/blob/main/open-api/rest-catalog-open-api.yaml).
