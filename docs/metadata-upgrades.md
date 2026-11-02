# Versioned metadata upgrades

Production web processes do not create tables or add columns. Alembic revisions
freeze schema operations in source. The API checks the required revision and refuses
to start against a pending or unknown schema. Customer data schemas are never migrated.

The Helm control-plane Deployment runs `python -m app.schema_migrations upgrade` in
an init container before API startup. PostgreSQL transaction advisory locks serialize
concurrent migration processes. Schema and legacy data changes run before the web
process starts. Future model changes must include a reviewed revision; a regression
test compares a freshly migrated schema with the ORM model.

For an external deployment, run the migration command as an operator-controlled job
using the metadata database credential, then start API replicas. Runtime database roles
can be separated from migration DDL authority by running the job with a migration
credential and checking schema versions using the runtime role.

## First adoption of an existing installation

1. Take and verify the metadata/key backup using the previous supported procedure.
2. Review the upgrade in a restored staging copy.
3. For a pre-versioned database only, run:

   ```sh
   python -m app.schema_migrations upgrade --adopt-legacy
   ```

   With Helm set `migrations.adoptLegacy: true` for this reviewed adoption, then
   return it to false. The frozen 0.6.1 baseline preserves existing tables/records,
   adds missing baseline tables/columns with explicit baseline defaults and retains
   the known warehouse column rename. A populated non-null field without an explicit
   backfill is refused. This is not permission to stamp an arbitrary database current.
4. Run `python -m app.schema_migrations check`, validate permissions and workloads,
   and review both Terraform roots for unintended changes.

Fresh installations need no adoption flag. Unsupported historic schema differences
need an explicit migration; do not use dynamic ORM schema repair in production.

## Failed upgrades and rollback

A PostgreSQL migration failure rolls back its transaction. The new API remains
unready; inspect sanitized failure status and investigate on the staging copy.
Do not automatically downgrade schemas or drop new metadata. An image rollback is
supported only when the release's compatibility tests establish that the older image
can operate against the resulting schema. Otherwise follow
[platform recovery](platform-recovery.md) into a separate empty target, using the
backup from before the upgrade. Recovery of external data remains customer-operated.

SQLite is an offline development/test backend, not the enterprise production migration
or transactional-DDL guarantee. CI separately exercises real PostgreSQL migrations and
recovery using matching client/server tools.

## Privacy snapshots and workspace grants

Drain active jobs before upgrading to the privacy-snapshot revision. New jobs capture
both admitted data authority and masks/row filters. Older runs without that snapshot
cannot acquire fresh SQL sessions; retry them under current policy. This avoids
reconstructing a less restrictive policy after a product grant changes.

Provision explicit workspace function grants before rollout. A Viewer page grant
shows navigation; it does not grant creation, execution or access to private files.
Existing workspaces keep their creator and captured owner group. Assign the
`workspaces` namespace on target `workspaces` in each permitted layer, or on a stable
`workspace:<id>` target for individual workspace operations. Private ownership still
applies. No implicit workspace Admin grant is introduced during migration.

Run content also needs explicit grants after upgrading: namespace `artifacts` Executor
for output payloads/rendered apps and `logs` Executor for console/log content, on the
workspace or collection target in the allowed layer. Output listing alone can use
`artifacts` Viewer. Existing private-run ownership is preserved without implicitly
adding these grants. Workload execution assignments retain their documented legacy
compatibility during migration.

## Private credentials and role separation

Revision `0007_private_credential_identity` binds personal secrets, Git credentials and
requester records to stable directory principal IDs. Recreating an account with the
same username does not transfer the previous person's credentials or requests.
Ambiguous legacy owners remain unbound and require operator review. Legacy Git host
fallback is retained only on migrated credentials that were bound successfully.
New Git credentials have no per-host legacy token fallback.

Provision `git.credentials` and `secrets` functions independently of page visibility.
Personal secret targets are `secret:user:<username>` (or the `personal-secrets`
collection); private identity ownership remains mandatory. Service-account secrets
inherit their actual service layer. Personal/group secrets remain prohibited in
production executions. See the explicit development grants in the Pokémon module.

Contributor does not imply Executor or Manager. Workload Contributors cannot run,
deploy or sync; API Contributors cannot call; Kafka Contributors can produce but
cannot consume. Data Contributors can write through governed SQL but cannot read
unless separately granted Executor. Native write-only credentials fail closed;
use governed SQL, or explicitly grant both reading and writing when required.
Package import publishes shared executable code and requires Manager, Admin, Owner
or the supported legacy Publisher/Maintainer assignment. Package Executor permits
installation of already approved packages.

Run metadata and retry also have independent functions. Retrying creates a new run
and rechecks current identity, data, secret and capacity authorization. Interactive
SQL continuations are bound to the active group captured at admission. Named SQL
grants must include `data.queries.results.read` to retrieve payloads and the relevant
query lifecycle permissions for status/cancellation; SELECT alone is not a result
retrieval grant.
