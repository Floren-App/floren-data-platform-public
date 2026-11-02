# Enterprise P0 implementation

Requested scope: finish authorization and object-level data boundaries, production
schema migrations, and customer-operated recovery with configurable S3-compatible
backup output. This document records implementation and verification; a checklist
entry is complete only after the relevant tests pass.

- [x] Encrypted metadata backup, dedicated S3 configuration and existing-integration reference.
- [x] Scheduling, history, permissions, API, Terraform and operator interface.
- [x] Offline verification, empty-target restore and recovery quarantine.
- [x] Versioned, serialized schema migrations and supported upgrade tests.
- [x] Fine-grained data authorization across SQL, native credentials and files.
- [x] Separate common-role and named-function checks for implemented public/internal operations.
- [x] Negative conformance tests, backend/browser regressions and generated documentation sources.

Customer databases, buckets, IdP and secret-store backups remain customer-operated.
FDP records these dependencies and provides a tested recovery procedure. A metadata
archive alone is not a backup of customer data or the entire installation.

## Verified implementation

Real PostgreSQL 16 validation covers migrations through `0007_private_credential_identity`,
concurrent upgrade serialization, encrypted archive upload/read-back, authenticated
decryption, empty-target restore, metadata preservation and recovery quarantine.
The isolated test namespace is deleted after validation. Backup unit tests also
cover malformed destinations, dedicated metadata database checks and archive tampering.

Object authorization covers SQL operations and column selection, exact file/directory
sessions, admission snapshots and active-group isolation. Jobs retain admitted masks
and row filters; metadata-only roles do not remove product masking. Shared metric
values are hidden when they bypass current data restrictions.

Workspace creation, lifecycle, file configuration reads, Git binding, editor gateway
and job submission have separate named permissions and layer checks. Metadata roles
cannot execute merely because they own a workspace. API trust/credential changes,
service-token operations, package downloads, Kafka produce/consume, workload controls
and metric definition/activation/reporting are checked separately.

## Delivery boundary

The P0 implementation is in source. Public publication, signed release/image CI,
installation upgrades and applying installation-specific Terraform are delivery
steps; these changes have not been published or deployed. Docker image construction
cannot be verified locally while the Docker daemon is unavailable. The Dockerfile
checks the matching PostgreSQL tools and client library during the release build.

The 511-row matrix includes reserved design operations without a dedicated API,
such as separate drafts, destructive infrastructure removal, custom SQL routines
and some export interfaces. A registry entry does not create that operation or
promise a connector supports it. Direct native object/column restrictions that
cannot be enforced safely are refused. The source-reference column and route
inventory make this distinction reviewable; neither replaces security review.

## Validation checkpoint (2 November 2026)

- Complete control-plane backend suite: 837 passed, 18 skipped, including SQL result
  scope, role separation, active-group/cache isolation and negative conformance checks.
- Complete production-preview browser regression suite: 118 passed, including metadata-only workspace,
  run and backup pages, credential controls and access-register exports.
- Python SDK: 29 passed; Terraform provider Go tests passed.
- Reusable Pokémon module: OpenTofu validation passed with the current locally built provider;
  no installation state, plan or apply was used.
- PostgreSQL 16 recovery round trip: passed; temporary namespace deleted.
- Desktop production build, Helm lint, Terraform formatting, strict MkDocs build and
  generated-site checks: passed (506 searchable documentation sections).

## Authorization completion details

Run metadata, retry, logs, artifacts and cancellation have independent checks.
Retries re-admit under current authority. SQL results require permission at every
selected table/column, and cursors are bound to the captured active group. Legacy
repository, warehouse and secret allowlists use selected effective app groups,
not inactive groups or editable service-account group labels.

Credential ownership and request ownership use immutable principal IDs; reusing a
username cannot transfer secrets, Git tokens or requester rights. Source/repository
metadata editing is separate from trust, destination, credential and removal rights.
Service identity functions use the actual service layer. Review duties remain an
additional constraint alongside named case/membership functions. Folder moves check
whether inherited authority changes. Updating a shared custom role rechecks every
bound assignment against the editor's delegation authority.

Contributor, Executor and Manager remain separate powers. API/Kafka Contributors
cannot call/consume, and workload Contributors cannot execute/deploy. Viewer can
inspect authorized metadata without receiving payloads or mutation rights. Existing
broad parent roles are additive: remove them when adopting narrower grants.

The Pokémon module now declares catalog discovery/request/form grants and private
development functions explicitly, alongside its separate data, product-owner and
stream-operator grants. Production development remains disabled by default.
