# Kafka implementation progress

Started 2026-10-30. Source proposal: `docs/kafka-plan.md`. Both files stay out of
public site navigation. Existing user changes and installation data are preserved.

Recommended proposal decisions selected: managed-first, Strimzi/KRaft, centralized
FDP policy enforcement, Apicurio contracts. Infrastructure stays opt-in; existing
layers do not gain brokers or consume additional capacity until explicitly enabled.

- [x] Phase 1 code: infrastructure contract, Strimzi manifests, prerequisites/readiness/retirement
- [x] Phase 1 code: governed topics, inherited roles, direct managed-identity grants, own consumer groups
- [x] Phase 1 code: authenticated broker callbacks, short-lived scoped client tokens, fail-closed policy
- [x] Phase 1 code: SDKs, stream workload lifecycle, catalog/lineage, scoped UI and OpenTofu
- [x] Phase 1 verification: real broker TLS/OAuth read/write/offsets, forged tokens, group/topic isolation and live revocation
- [x] Phase 1 verification: current-generation readiness, finalized deletion, safe topic-name reuse and retained-compute retirement logic
- [x] Portable example: managed TCGDex API → Python producer → topic → stream → JSON-array S3 batches; separate Test/Prod IaC and replay tests
- [ ] Phase 1 live Kubernetes acceptance: operator reconciliation, broker image, CNI enforcement, PVC restart/retirement/reactivation
- [ ] Phase 2: isolated Connect workers/identities, plugin allowlist, CDC and Iceberg sinks
- [ ] Phase 2: Apicurio schema contracts, compatibility, enforced-path limitations
- [ ] Phase 3: authenticated HTTP bridge, optional broker listeners, external clusters
- [ ] Phase 4: consumer lag, quotas, freshness/SLAs, autoscaling and operations
- [ ] End-to-end example and live acceptance with promotion/layer isolation

## Current checkpoint

Phase 1 is implemented in the working tree. The full four-phase proposal is NOT
complete. There has been no Kafka commit/push, platform upgrade, broker deployment
or modification of the existing private Pokémon installation in this task.

The cluster is reachable, but has no Strimzi operator/CRDs installed. The local
Docker daemon was unavailable; the plugin JAR and real broker were built/tested
with Java/Maven directly instead. Do not mistake that for a built/published Docker
image or a live Strimzi/network/storage acceptance test. R wrapper/image changes
have not been exercised in an R container locally.

Core implementation:

- `services/control-plane/app/layer_kafka.py`, `layer_infrastructure.py`,
  `layer_manifests.py`, `layer_controller.py`: opt-in broker resources, exact-host
  API egress for the topic operator, restricted operands, warehouse sizing/counts,
  retained PVCs, TLS readiness and prerequisite checks. Quorum/storage changes are
  refused without an explicit migration. Production needs at least three brokers.
- `kafka_access.py`, `routes/kafka.py`, `models.py`: scoped native sessions,
  live IAM checks, lifecycle API/controller reconciliation, safe tombstones,
  current/future inherited topic grants and managed identity bindings.
- `images/kafka/authorizer`: compiled Java authorizer with authenticated,
  size/time-bounded callbacks. No allow cache/fail-open path. The server-only JAAS
  subject is separate from strict client JWT signature/type/issuer/audience checks.
- `sdk/python/fdp/kafka.py`, `streaming.py`, and R wrappers/images: JSON clients,
  token refresh, explicit consumer commits, observed lineage, continuous readiness
  and termination. `kind=stream` uses a fixed managed identity, not a caller, and
  has no public app endpoint or on-demand run action.
- `apps/desktop/src/views/Kafka.tsx`: Storage cluster/topic browsing, on-open topic
  fetches, per-topic IAM/settings/lineage, active-group scoping and topic-only grants.
- `terraform/provider-fdp/internal/provider/kafka.go`: `fdp_kafka_cluster` and
  `fdp_kafka_topic`, import/read/drift and real readiness/deletion waits.
- `examples/kafka/`: independent portable source and Test/Prod module. It is not a
  Kafka Connect substitute and does not pretend TCGDex exposes card-price changes.
- `.github/workflows/pipeline.yml`: native broker integration gates image publication;
  Kafka image included in normal signed/provenance builds. No workflow was pushed.

Related security fix: verified external IdP usernames cannot select internal
identities through the reserved `service:` or `sa:` prefixes. Actual managed
execution identities continue using their existing token path. Signed-JWT tests
cover the distinction.

Verification on 2026-10-30:

- Full control plane, including the real broker test: **702 passed, 17 skipped**.
- Python SDK, including replay/traceability and stream health: **22 passed**.
- Java plugin compilation, packaging and callback tests: passed (Kafka 4.3.1,
  Strimzi OAuth 0.18.1 test runtime, shaded Jackson 2.22.1).
- Go provider tests/vet and generated schema reference: passed.
- TypeScript/Vite build and Helm lint: passed.
- Strimzi manifests validated against the pinned operator's v1 CRD schemas.
- Strict Markdown site build/check passed; the proposal, this log and the private
  operating notes are excluded from generated pages/search.
- Kafka browser test passed. The first full browser run found two stale assertions;
  both reproduced against an isolated copy of unchanged HEAD. They were updated
  for the existing Services execution-identity column and Documentation navigation.
  Final full browser result is recorded below when it finishes.

Local verification logs live under `/private/tmp/fdp-kafka-*.log` and may disappear.
The reproducible broker commands and installation/key-rotation procedure are in
`docs/kafka-operations.md`. CI downloads its own test dependencies; no developer
machine paths are required.

## Continue here

1. Build/publish the Kafka and updated runtime/control-plane images through the
   normal reviewed source/release process. Preserve unrelated existing user edits.
2. Complete Phase 1 Kubernetes acceptance in a dedicated Non-Prod environment:
   optional Strimzi install, issuer/trust, exact API CIDRs, owned label propagation,
   generated network policies, denied cross-layer access, retained broker restart,
   retirement/reactivation and recovery after partial reconcile failure. Do not
   claim the real JVM test proves CNI or persistent-volume behavior.
3. Implement Phase 2 with separate connector identities and private internal topics.
   Debezium replication privilege/publication scope and Iceberg control-topic /
   transactional permissions need explicit design/tests; never grant a shared worker
   unconstrained access to all tenant topics. Credentials must resolve at runtime.
4. Add Apicurio compatibility and validated ingestion paths. Native clients can
   bypass serializers: registry registration alone is not payload enforcement.
5. Implement Phase 3 external access/ACL lifecycle, then Phase 4 measured lag,
   per-identity quotas and autoscaling. Current topic freshness comes from SDK
   observations, not authoritative broker message-age measurements.
6. Integrate and run the complete Pokémon stream example with existing promotion,
   separate layer groups and analyst-only curated access. Do not invent prices.

Technical findings:
- Strimzi's current v1 API uses custom authentication/authorization; do not rely on
  removed/deprecated convenience fields or install an unpinned broker plugin.
- Policy callbacks must authenticate the broker as well as receive an asserted
  principal; a reachable unauthenticated policy endpoint would permit spoofed calls.
- Schema-registry compatibility is not broker-side message validation. Native
  clients can bypass a serializer. Enforced paths need explicit validation; never
  claim all writes are validated merely because an Apicurio registry exists.
- Consumer revocation must be bounded by policy/token lifetime for long-lived streams;
  the batch-job rule allowing admitted work to finish cannot grant infinite access.
