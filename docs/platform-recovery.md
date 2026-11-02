# Platform backup and recovery

FDP provides recovery methods for its own state. Customers operate the destination,
backup schedule and infrastructure tooling. Registered business databases and buckets
are **not** copied by a platform backup.

The metadata database must be dedicated to FDP. Before dumping, the backup worker
checks for unknown user tables, views, materialized views and foreign tables and
refuses a database containing them. Keep business data in storage integrations,
not alongside the control-plane tables.

## Recovery boundary

| State | Protection |
|---|---|
| FDP metadata: permissions, catalog, access cases, configuration, run history | Encrypted platform metadata archive |
| Platform session/Trino signing keys, previous keys, gateway/proxy tokens and audit signing key | Inside that encrypted archive; restored into an operator-only file |
| External databases, Iceberg data and REST catalog, customer buckets | Customer's native backups and recovery tools |
| Identity provider and directory | Customer's IdP backup/provisioning process |
| Secret-store contents, authentication, unseal/KMS configuration | Customer's secret-store recovery process |
| Workspace files, artifacts, uploaded packages, Airflow databases and Kafka state | Customer's volume/application-aware backups |
| Git and Terraform state | Customer's protected source and state storage |
| Containers, caches and temporary compute | Recreate; they are not archived |

A retained volume, Kafka replication or Git configuration alone is not a complete
backup. Each archive has a dependency manifest. It deliberately contains no business
data export, no secret-store scrape and no IdP database dump. Platform backup state
must be protected as sensitively as the platform itself.

## Configure an S3-compatible destination

Use Administration → Backup and recovery, `PUT /v1/backups/configuration`, or Terraform.
Configuration and quarantine approval require **active Global Admin**, because changing
the destination or recovery key controls archives of the entire installation.
Delegating `backups.create` permits archive creation; it does not return plaintext or
the private recovery key. Page visibility additionally needs `pages` Viewer on
`set-backups`. History/configuration reads use their separate named permissions.

Generate a customer-held recovery key, for example:

```sh
openssl genpkey -algorithm RSA -aes-256-cbc -pkeyopt rsa_keygen_bits:3072 -out recovery-private.pem
openssl pkey -in recovery-private.pem -pubout -out recovery-public.pem
```

Keep the encrypted private key and its passphrase outside FDP and outside its cluster.
Only the public key is configured in FDP. Use a bucket outside the platform's failure
domain, preferably in a separate account with restricted recovery operators.

```hcl
resource "fdp_backup_configuration" "platform" {
  enabled             = true
  bucket              = "company-platform-recovery"
  prefix              = "production/fdp"
  region              = "eu-west-1"
  interval_hours      = 24
  recovery_public_key = file("recovery-public.pem")

  # Optional, in addition to mandatory client-side encryption:
  server_side_encryption = "aws:kms"
  kms_key_id             = var.recovery_kms_key_arn
  # Set only for a bucket already configured for Object Lock:
  object_lock_days       = 30
}
```

The dedicated destination uses the installation's AWS SDK credential chain; prefer
its workload identity. For S3-compatible storage set an HTTPS `endpoint_url` and the
signing `region`. Configure credentials through the installation's supported secret
injection, never in this Terraform resource or a workload manifest.

Alternatively reference an existing **external S3 files integration**:

```hcl
resource "fdp_backup_configuration" "platform" {
  source_id           = fdp_data_source.recovery.id
  prefix              = "platform-backups"
  recovery_public_key = file("recovery-public.pem")
  interval_hours      = 24
  enabled             = true
}
```

This uses the integration's bucket, endpoint, base prefix and STS provisioning
credential. Its assumed role is narrowed to backup object operations beneath that
prefix. No source credentials enter Terraform state or the archive inventory.
For integration-backed SSE-KMS, specify `server_side_encryption = "aws:kms"` and an
explicit KMS **key ARN**; the session also needs GenerateDataKey/Decrypt on that key.
A dedicated integration with a dedicated role is recommended. Layer-local managed
RustFS is refused as a referenced recovery destination.

The role needs PutObject/GetObject/AbortMultipartUpload on the destination prefix,
plus PutObjectRetention if Object Lock is requested. Do not grant DeleteObject just
to run backups. Customer S3 lifecycle rules control expiry. FDP never deletes old
archives or configures a bucket lifecycle silently. COMPLIANCE Object Lock retention
cannot be shortened by an application operator. The bucket must already support it.

Destroying `fdp_backup_configuration` disables future scheduling and retains archives
and inventory. Manual backup creation uses the retained configuration. Already
admitted work is not erased by destroying a Terraform configuration resource.

## Archive and verification

A leader schedules backups; manual creation uses `POST /v1/backups`. One backup is
admitted at a time. Its destination and public key are frozen at admission. A
PostgreSQL advisory lock also prevents concurrent dumps across leader failover.

The archive contains a custom-format metadata `pg_dump`, dependency manifest and
platform key recovery file. A fresh AES-256-GCM key encrypts each archive; RSA-OAEP
wraps it for the customer's recovery public key. Temporary plaintext is confined to
a private temporary directory and removed on completion/failure. Use encrypted
node/ephemeral storage for production; unlinking a file does not guarantee physical
erasure from a disk.

Success requires uploading and reading back the entire ciphertext with a matching
SHA-256 and size. Inventory reports the S3 object, checksum and status, never a
presigned plaintext download. Completion/failure is audited. Preserve the checksum
in trusted external audit evidence or a protected operator inventory: possession of
a public encryption key does not prove who produced an archive.

The shipped runtime contains PostgreSQL **16** dump/restore tools, matching bundled
metadata PostgreSQL. Recovery checks the client/server major versions and refuses a
mismatch. For a different external PostgreSQL major version, supply matching tools
in the installation/restore environment (`FDP_POSTGRES_BIN_DIR`). Test the exact
version combination before adopting it.

Monitor `fdp_backup_failing` and
`fdp_backup_last_success_timestamp_seconds`. Alert when the latest attempt fails or
no successful backup exists within the agreed recovery-point window. A successful
upload does not establish the installation's recovery-time objective; rehearse it.

## Verify and restore offline

Download the encrypted object using the customer's recovery identity. Obtain its
SHA-256 from trusted inventory or independent audit evidence. Supply the private key
passphrase through `FDP_RECOVERY_KEY_PASSWORD`, without putting it in shell history.

```sh
python -m app.recovery verify \
  --archive metadata.fdp \
  --private-key-file recovery-private.pem \
  --expected-sha256 <trusted-ciphertext-checksum>
```

This authenticates the entire encrypted envelope before inspecting its contents.
Unexpected members, paths, links, checksum mismatches and corrupt ciphertext are
refused. Verification does not contact customer data systems.

For recovery:

1. Prepare a **separate empty PostgreSQL database**. Stop controllers/API replicas
   intended to use it. Do not run the schema initializer against it first.
2. Set `FDP_RESTORE_DATABASE_URL` using the operator's secret injection. Restore:

   ```sh
   python -m app.recovery restore \
     --archive metadata.fdp \
     --private-key-file recovery-private.pem \
     --expected-sha256 <trusted-ciphertext-checksum> \
     --confirm-empty-target \
     --keys-output /protected/platform-keys.json
   ```

   Any existing user tables/views/sequences cause refusal. `pg_restore` is transactional,
   with ownership and ACL restoration disabled. The keys output is create-only and
   mode 0600. Restore those keys into the platform's existing-secret mechanism using
   a protected operator process; the JSON file is not a Kubernetes Secret manifest.
3. Recover or reconnect the customer-managed IdP, secret store, storage, volumes and
   application state. Restore compatible Helm values, Git and Terraform state. Keep
   the new installation isolated from ordinary traffic during reconciliation.
4. Start the matching FDP release. Restored metadata is **quarantined**: ordinary
   operations and background scheduling are blocked. Layer reconciliation also waits.
   Workloads/workspaces are stopped, old query admissions expired and admitted runs
   stopped. Restoring metadata never intentionally replays pipeline work.
5. Reconcile current directory membership, disabled accounts, revoked grants and
   expired approvals. Review changes since the backup; an old backup can contain
   authority revoked after its creation. SCIM and the recovery administration API
   remain available. Protect this maintenance endpoint with the installation's network
   access controls.
6. An active Global Admin approves `POST /v1/backups/recovery/resume`, affirming
   `directory_reconciled`, `permissions_reviewed`, `expired_access_reconciled` and
   `secrets_and_data_verified`. All must be true. This is an audited operator
   attestation, not an automatic proof that external systems are consistent.
7. Restart API/controller replicas, review the Terraform plan, validate real allowed
   and denied accesses, and explicitly redeploy/start the intended workloads. Check
   customer Kafka/Airflow state before resuming schedules. Complete an application
   smoke test before reopening ordinary traffic.

If recovery fails, retain the old installation and recover into another empty target.
Never overwrite a functioning database or delete customer data to force convergence.

## Automated acceptance

`python scripts/verify-platform-recovery.py` uses
`FDP_RECOVERY_TEST_POSTGRES_URL` pointing at an isolated disposable PostgreSQL server.
It creates two random databases and removes only those databases in `finally`.
It tests concurrent migration processes, real pg_dump/pg_restore, envelope verification,
metadata preservation, quarantine and refusal of a populated target. The S3 adapter
is in-memory in this test; customer endpoint/IAM/Object Lock acceptance remains an
installation check. CI runs this test against PostgreSQL 16 before image publication.

## Customer-managed PostgreSQL recovery

Customer PostgreSQL snapshots or point-in-time recovery are also supported operationally.
Keep FDP APIs and controllers stopped while restoring. Restore the matching external
secret-store material and application keys, then run any required offline metadata
migration. Before starting FDP, explicitly quarantine the restored target:

```bash
python -m app.recovery quarantine --confirm-restored-target
```

The target comes from `FDP_RESTORE_DATABASE_URL`. This command requires the current
metadata schema. It stops restored runtime records, expires SQL admissions and enters
the same reconciliation gate as archive recovery. Never point it at the running primary.
Customer-native recovery does not automatically provide FDP application keys; recover
those from the customer secret store or a matching encrypted FDP archive.

Each completed recovery creates a new audit `recovery_epoch`, included in SIEM events.
Partition exported hash-chain verification by installation and recovery epoch; the
new stream starts with sequence one and the zero predecessor hash. Retain the prior
external audit stream as evidence rather than treating restored history as new events.
An independent database guard blocks startup traffic if archive restore finalization
fails. Recover into another empty target after investigating the failure.

If database restore succeeded but writing the local keys file failed, keep the recovered
database quarantined. Extract keys separately from the verified archive:

```bash
python -m app.recovery extract-keys \
  --archive platform.fdp --private-key-file recovery-private.pem \
  --expected-sha256 "$TRUSTED_ARCHIVE_SHA256" --keys-output recovered-keys.json
```

Extraction creates a mode-0600 file and refuses to overwrite an existing path.
