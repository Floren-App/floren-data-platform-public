# Operating Floren Data Platform on Kubernetes

Upgrades, rollback, backup and restore, key rotation, retiring layers and diagnostics. Every
procedure here is exercised by `make test-e2e-install` on a disposable cluster, as noted.
Installation is in [kubernetes-install.md](kubernetes-install.md).

## Upgrade and rollback

```sh
make preflight                                   # the cluster still meets the prerequisites
helm upgrade fdp deploy/helm/fdp -n fdp -f my-values.yaml --wait --timeout 10m
tofu plan                                        # configuration unchanged by the upgrade
```

Generated keys and passwords are kept across upgrades (they live in retained Secrets), and so
are the metadata database, layer volumes and deployed workloads. Layers are reconciled again
by the controller after it restarts.

To go back to the previous release:

```sh
helm history fdp -n fdp
helm rollback fdp <revision> -n fdp --wait --timeout 10m
```

Rollback restores the previous chart and images; the test confirms secrets, metadata and
deployed apps are unchanged and OpenTofu sees no drift. **Database migrations are additive**
(new tables and nullable columns), so the previous version runs against the newer schema. If a
release notes a non-additive migration, take a metadata backup first and restore it after a
rollback.

## Backup and restore

| What | Where | How |
|---|---|---|
| Platform metadata (configuration, permissions, run history, catalog) | Bundled: `fdp-metadata-db` StatefulSet; external: your PostgreSQL | `pg_dump` (below) |
| Platform keys and passwords | Secrets `fdp-control-plane`, `fdp-keycloak`, `fdp-metadata-db` | Back up with your cluster backup tool (encrypted) |
| Application secrets (bundled OpenBao) | Raft data in namespace `<release>-openbao` | `bao operator raft snapshot save`, plus the `fdp-openbao-seal` Secret (Kubernetes model) |
| Application secrets (Kubernetes backend) | Namespace `<release>-secrets` | Same as above |
| Application secrets (your OpenBao, Vault or cloud secret manager) | Your secret store | Its own backup tooling |
| Identity provider | Bundled Keycloak database in the metadata server | Included in a full `pg_dumpall` |
| Layer artifacts and workspace volumes | PVCs in each `fdp-<installation>-layer-<id>` namespace | Volume snapshots |

Metadata backup and a restore check:

```sh
kubectl -n fdp exec fdp-metadata-db-0 -- pg_dump -U postgres -d fdp --no-owner --no-privileges > fdp.sql
# Verify it restores, without touching the live database:
kubectl -n fdp exec fdp-metadata-db-0 -- psql -U postgres -c 'CREATE DATABASE restore_check'
kubectl -n fdp exec -i fdp-metadata-db-0 -- psql -q -U postgres -d restore_check -v ON_ERROR_STOP=1 < fdp.sql
kubectl -n fdp exec fdp-metadata-db-0 -- psql -U postgres -c 'DROP DATABASE restore_check'
```

The test restores a dump this way and compares row counts of workloads, runs, governed
objects, role assignments, layer operations and identities. To recover from data loss, scale
the control plane and controller to zero, restore the dump into an empty `fdp` database,
and scale them back; the controller then reconciles every layer from the restored state. (The
test proves dumps restore completely; this full replacement of the live database is not yet
exercised by it.)

Secrets must be restored together with the metadata they belong to: signing keys, the
Trino token key and stored credentials are not in the database dump.

## Rotating keys

Signing keys rotate with an overlap: former keys still verify, but never sign, until you
remove them. Edit the platform Secret (`fdp-control-plane`):

| Key | New value in | Former value in | Remove the former value after |
|---|---|---|---|
| Session/execution token key | `FDP_SESSION_SIGNING_KEY` | `FDP_SESSION_SIGNING_PREVIOUS_KEYS` (comma-separated) | Tokens it signed expired: deployed apps and editor proxy tickets can last 30 days; redeploy apps/restart editors or retain the old verification key until expiry |
| Layer Trino token key (EC, PEM) | `FDP_TRINO_JWT_PRIVATE_KEY` | `FDP_TRINO_JWT_PREVIOUS_KEYS` (PEMs concatenated) | One hour (query tokens are short-lived) |

```sh
kubectl -n fdp patch secret fdp-control-plane --type merge -p '{"data": {...}}'
kubectl -n fdp rollout restart deployment/fdp-control-plane
```

Layer Trinos fetch the public keys from the control plane, so they need no restart. The test
rotates both keys, then runs a query on a layer's Trino with the new key and an Airflow
deployment that still holds a token signed with the former key; both succeed.

Other credentials:

- **Keycloak client secrets and SCIM token:** set a new value in Keycloak and in the Secret,
  then restart the control plane and the directory sync.
- **Data-source and Git credentials:** set the new value and bump `credentials_wo_version` in
  OpenTofu (or save it in the desktop app); it is never stored in state.
- **Certificates:** see [public certificates](../deploy/helm/fdp/README.md#public-certificates)
  and [layer Trino HTTPS](../deploy/helm/fdp/README.md#layer-trino-https); cert-manager
  renews them and Trino reloads them without a restart.

## Certificate monitoring and issuer recovery

The platform watches every certificate it serves, from a verified TLS handshake (it never reads a
Secret): both gateway certificates (every `certificateMonitoring.intervalSeconds`, sooner while
one needs attention) and each layer's Trino and PostgreSQL certificates (on every layer
reconciliation, about once a minute), combined with cert-manager's status when it issues them.

| State | Meaning | Severity |
|---|---|---|
| `ok` | Valid; cert-manager renews it at the shown time, or it has more than 14 days left | 0 |
| `renewing` | cert-manager is issuing its replacement | 0 |
| `expiring` | Less than 14 days left and nothing renews it (existing or externally managed certificates) | warning |
| `renewal_failed` | cert-manager reported a failure, or an issuance has been pending longer than 30 minutes (typically an issuer that is not ready) | warning |
| `renewal_overdue` | Renewal was due over an hour ago and has not happened | warning |
| `unverified` | The endpoint did not present a certificate the platform CA bundle trusts | warning |
| `expired` | Expired | critical |

`GET /api/v1/admin/certificates` lists them, most severe first; a layer's own appear in
`GET /api/v1/admin/layers/<id>/infrastructure`. For alerting, Prometheus can scrape
`http://fdp-control-plane.<namespace>.svc:8000/internal/metrics/certificates` with
`Authorization: Bearer $FDP_METRICS_TOKEN` (generated into the platform Secret by bootstrap;
add it yourself with an external Secret; without it the endpoint is off). It exposes
`fdp_certificate_expiry_timestamp_seconds`, `fdp_certificate_problem` (1 with a
`state` label when it needs attention) and `fdp_certificate_checked_timestamp_seconds`.
Example rules:

```yaml
groups:
  - name: fdp-certificates
    rules:
      - alert: FDPCertificateProblem
        expr: fdp_certificate_problem == 1
        for: 15m
        annotations: { summary: "{{ $labels.certificate }} is {{ $labels.state }}" }
      - alert: FDPCertificateExpiresSoon
        expr: fdp_certificate_expiry_timestamp_seconds - time() < 7 * 86400
      - alert: FDPCertificateCheckStale
        expr: time() - fdp_certificate_checked_timestamp_seconds > 3600
```

**Issuer recovery.** A failing issuer does not interrupt service: the current certificate keeps
being served (layers stay Ready) while the state is `renewal_failed`. Fix the issuer (its CA
Secret, ACME account or credentials) until `kubectl get issuer` reports Ready, then trigger the
renewal (`cmctl renew -n <namespace> <certificate>`); cert-manager otherwise retries with
backoff. The new certificate is reloaded without restarts and the state returns to `ok`.
`make test-certificate-renewal` checks the renewal time equals `notAfter - renewBefore`, breaks
the issuer, sees `renewal_failed` while the old certificate keeps serving, repairs it and sees
the new certificate and `ok`. `make test-e2e-install` checks the gateway certificates through the
admin API and the metrics endpoint.

## Secret backend recovery (OpenBao)

**Bundled OpenBao.** Pods unseal themselves; a pod that stays sealed or not ready usually lacks its key:
in the Kubernetes model, check that the `fdp-openbao-seal` Secret exists and that the seal-key init
container succeeded; in the KMS model, check the pods' KMS identity and network access. Never delete or
replace the seal Secret: without the original key the data cannot be decrypted. Restore a lost key from
its backup.

The chart keeps no OpenBao root token. For administration (snapshots, policy changes, seal key rotation),
log in through the break-glass service account `fdp-openbao-admin`. No pod runs as it; Kubernetes RBAC
decides who may create its tokens. The OpenBao token is valid for at most an hour and every request is
audited. For example, to take a Raft snapshot:

```sh
JWT=$(kubectl -n fdp-openbao create token fdp-openbao-admin --duration=10m)
kubectl -n fdp-openbao exec -i fdp-openbao-0 -c openbao -- sh -c '
  export BAO_TOKEN=$(bao write -field=token auth/kubernetes/login role=admin jwt="$(cat)")
  bao operator raft snapshot save /tmp/openbao.snap >/dev/null && cat /tmp/openbao.snap && rm /tmp/openbao.snap
' <<< "$JWT" > openbao.snap
```

Store snapshots encrypted, together with a copy of the seal key in the Kubernetes model.

With `secretManagement.backend: openbao` or `vault`, secret values live only in that server. When it is
unavailable the platform stays up; anything that reads or writes a secret answers **503** with
the cause, never OpenBao's own response:

| Message | Cause | Recovery |
|---|---|---|
| `OpenBao is sealed or starting` | OpenBao restarted | Unseal it (`bao operator unseal`); requests succeed again at once, no platform restart |
| `OpenBao refused the platform token` | The token expired, was revoked or lost its policy | Issue a new token with the platform policy (see [installation](kubernetes-install.md#using-your-own-postgresql-identity-provider-and-openbao)), set `FDP_OPENBAO_TOKEN` in the platform Secret, `kubectl rollout restart deploy/fdp-control-plane` |
| `Cannot reach OpenBao` | Network or address | Check `openbao.addr` and network access from the platform namespace |

Back up OpenBao with its own tooling (storage snapshots); restoring it restores every secret
the platform stored. `make test-e2e-external` restarts OpenBao sealed, checks the 503 and that the
platform stays up, unseals it and checks secrets survived, then revokes the platform token and
recovers with a new one.

## Retiring and deleting a layer

Retiring scales a layer's compute to zero and keeps everything else:

```sh
POST /api/v1/admin/layers/<id>/infrastructure/retire   {"expected_generation": <current>}
```

It is refused while the layer still has live workloads, running workspaces or active
runs. After retirement the layer refuses new work, and its namespace, volumes and network
policies stay. Reactivate it by applying the infrastructure again with `retired: false`.

Deleting the retained data is a deliberate operator step, never done by the platform:

```sh
kubectl delete namespace fdp-<installation>-layer-<id>
```

The controller notices within about a minute and reports `StorageDeleted`, and never
recreates the namespace; only then can the layer itself be deleted. The default layer cannot be
deleted until another layer is the default. The test retires a spare layer, checks it scaled to zero with
its volume kept, deletes its namespace and the layer.

## Diagnostics

| Symptom | Look at |
|---|---|
| A layer is not Ready | `GET /api/v1/admin/layers/<id>/infrastructure` (`phase`, `reason`, `message`), then `kubectl -n fdp logs deploy/fdp-layer-controller` |
| A deployment did not go live | The workload's events and `sync_error` (desktop app or `GET /api/v1/workloads/<id>`) |
| Work is refused with `LayerNotReady`, `LayerRetired`, `LayerQuotaExceeded` or `ManagedLayerRequired` | The message names the cause; quota and readiness are per layer |
| The platform is not Ready | `kubectl -n fdp describe pod -l app.kubernetes.io/name=fdp-control-plane` (`/readyz` reports the failing dependency) |
| People cannot sign in | Directory sync logs: `kubectl -n fdp logs deploy/fdp-directory-sync` |
| Isolation doubts | `make preflight` (NetworkPolicy enforcement) |

Common controller reasons:

| Reason | Meaning |
|---|---|
| `WaitingForRollout` | Trino or the artifact holder is starting; check pod events, images and quota |
| `RollingUpdate` (phase Ready) | An already-ready layer is rolling pods of the same configuration, e.g. after a platform upgrade; work stays admitted |
| `WaitingForCertificate`, `WaitingForVerifiedTLS` | Certificate issuance or trust; check the issuer and the platform CA bundle |
| `NetworkBoundaryMissing` | Set `layerController.network` CIDRs before granting data-source egress |
| `OwnershipConflict` | A Kubernetes object with the same name belongs to another installation; it is never adopted |
| `InsufficientQuota` | The Trino coordinator plus the maximum number of workers do not fit the layer quota |
| `WorkerWarehouseMissing` | The layer's Trino worker warehouse (`trino.workers.warehouse`) is unset or was removed |
| `WorkerWarehouseTooSmall` | The worker warehouse has less than 1 GB memory |

Trino worker scaling: every 15 seconds the control plane reads running and waiting queries from each
ready layer's coordinator (`/metrics`) as the reserved identity `fdp-monitor`. The layer policy lets that
identity read system information and nothing else; its tokens are signed by the platform's layer key like
every other query token. The decided count is stored on the layer and applied by the layer controller.
A worker being removed (scale-down, upgrade or node drain) runs a `preStop` hook that asks Trino locally to
shut down gracefully, so it takes no new work and finishes its running queries first. `trino_workers.error`
on the layer infrastructure state says when load could not be read; the workers then stay as they are.
| `StorageRetained`, `StorageDeleted` | A retired layer; see above |
