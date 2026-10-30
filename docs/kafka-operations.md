# Managed Kafka: operating the first phase

Implementation notes, deliberately outside public site navigation. The proposal in
`kafka-plan.md` remains unchanged. See `kafka-implementation.md` for the delivery
checklist and outstanding work; this is not a claim that all four phases shipped.

## Installation

Kafka is opt-in. Existing layers keep their current infrastructure until changed.
The tested combination is Strimzi 1.2.0, Kafka 4.3.1, Java 21 and the FDP Kafka image
built from `images/kafka/Dockerfile`. A stock Kafka image lacks the FDP authorizer.

Install one Strimzi operator using `kafkaOperator.enabled: true` in the FDP chart,
or retain an existing compatible operator. Do not install competing operators.
The optional chart dependency includes the v1 CRDs. CRD upgrades need the normal
operator upgrade procedure; Helm does not upgrade installed CRDs automatically.

Set `layerController.network.kafkaApiServerCidrs` to exact Kubernetes API service
and endpoint addresses: `/32` for IPv4, `/128` for IPv6. This allows only the topic
operator to reach API ports 443/6443. Ordinary workloads receive no API access.
The remaining layer default-deny rules, namespace boundaries and client TLS still
apply. Strimzi is a trusted cluster operator; its permissions belong to installation
administration, not to tenant groups.

Configure a separate broker warehouse. Warehouses reserved for Trino cannot host
Kafka. Each broker takes one warehouse slot, with that warehouse's CPU/memory.
Choose at least 2 GB per broker for initial operation and size from measured load.
A one-broker Non-Prod lab is supported; production requires at least three brokers
and a replication factor of at least three. Combined controller/broker nodes are
used. This implementation does not promise production performance from defaults.

The layer's `fdp_layer.infrastructure` JSON accepts:

```hcl
kafka = {
  enabled   = true
  image     = "your-registry/fdp-kafka:tested-commit"
  version   = "4.3.1"
  warehouse = fdp_compute_pool.kafka.name
  replicas  = 3
  size      = "50Gi"
  tls = {
    mode        = "cert_manager"
    secret_name = "kafka-tls"
    issuer      = { name = "layer-issuer", kind = "Issuer" }
  }
}
```

Merge this with the layer's existing infrastructure configuration. Configure the
issuer in the layer namespace first. Alternatively supply an existing `kafka-tls`
Secret containing `tls.crt` and `tls.key`. The certificate needs the bootstrap
service DNS and broker headless-service DNS names; see the generated Certificate.
The issuing CA must be in the platform's public trust bundle, which is propagated
to SDK runtimes. Broker private keys stay in the layer.

The control plane and controller use the existing platform signing key to derive
a separate broker policy credential for each layer. The controller creates the
layer Secret once; it never reads it back. Kafka clients get ES256 JWTs backed by
the public JWKS, rather than that broker credential or a shared Kafka password.
The server-only JAAS login callback is separate from client JWT validation: its
non-JWT placeholder cannot authenticate a network client. Signed token type,
issuer, audience and expiry checks remain enabled.

Client traffic uses TLS. Broker-to-control-plane policy/JWKS requests use the same
internal HTTP/network-trust model as the existing Trino policy integration; this
is not end-to-end encryption of the control-plane network. No native external
listener is exposed in this phase.

## Topics and IAM

Register enabled layer Kafka in Storage → Register Kafka, or:

```hcl
resource "fdp_kafka_cluster" "events" {
  layer_id = fdp_layer.production.id
}

resource "fdp_kafka_topic" "cards" {
  cluster_id         = fdp_kafka_cluster.events.id
  name               = "pokemon.cards"
  partitions         = 3
  replication_factor = 3
  retention_ms       = 604800000
  cleanup_policy     = "compact,delete"
}

resource "fdp_role_assignment" "engineers" {
  resource_id = fdp_kafka_cluster.events.id
  app_group_id = fdp_app_group.engineers.id
  role = "Admin"
}
```

Reader consumes and commits offsets for its own groups. Contributor also produces
and uses idempotent writes. Admin additionally manages topic lifecycle through
the FDP API; Owner also assigns permissions. Native ACL administration and native
topic DDL remain denied to client tokens. Grants on a cluster inherit to topics;
additional topic grants never block inherited access. Direct managed-identity
assignments work without an intermediary group.

The active group owns newly created registrations/topics. Creation requires that
primary group's permission; secondary groups may authorize other operations.
Topic-only grants reveal the cluster name needed for navigation, but no unrelated
topic names or grants. Children load when the cluster is opened.

Consumer groups are prefixed with the actual principal ID. Give two instances
the same logical group only when they share a managed identity and intentionally
share consumption. Different identities cannot take over each other's offsets.
Tokens last at most five minutes and are scoped to one topic, mode and layer.
The authorizer checks current IAM and execution activity without an allow cache;
revocation denies subsequent broker operations, including already connected
clients. A control-plane failure denies access. This availability dependency is
deliberate and must be included in production capacity/availability planning.

Topic create/update waits for Strimzi's current generation to become Ready.
Partition counts can increase, not decrease. Storage/quorum changes and replica
reassignment need an explicit migration, not a destructive automatic replacement.
OpenTofu deletion waits for the KafkaTopic finalizer to finish before removing
state. Reusing a deleted topic name creates a new resource ID: previous grants
and sessions do not carry into the replacement topic.

Unregistering an empty cluster removes its discoverable metadata, not its brokers
or volumes. Re-registering restores the existing integration/ownership. Retirement
pauses Strimzi reconciliation, then stops owned pod sets and entity deployments;
PVCs remain. Reactivation lets Strimzi restore compute. Delete topics while their
layer is active; retired layers intentionally do not reconcile data deletion.

## Programs and lineage

Install `fdp[kafka]`, then:

```python
import fdp

with fdp.kafka.producer("pokemon.cards", layer_id="layer-id") as producer:
    producer.send({"record_id": "demo-card", "event": "observed"}, key=b"demo-card").get(timeout=30)

with fdp.kafka.consumer("pokemon.cards", group="card-loader", layer_id="layer-id",
                        auto_offset_reset="earliest") as consumer:
    for record in consumer:
        persist_idempotently(record.value)
        consumer.commit()
```

Producers default to JSON serialization, idempotence and all-replica acknowledgement.
Consumers default to JSON deserialization and explicit offset commits. Commit after
durable idempotent processing: this does not provide an atomic transaction between
Kafka and SQL/S3 or promise exactly-once delivery across systems.

The SDK obtains/refreshes tokens automatically on authentication/reconnection;
the broker bounds connection lifetime. Native access continues to enforce the
same topic/mode grants if someone bypasses the convenience wrapper.

R uses `fdp::kafka_producer()` and `fdp::kafka_consumer()` through reticulate and
the same Python SDK. Updated FDP R images install both. Custom R runtime images
need reticulate and a Python environment containing `fdp[kafka]`.

Publish long-running consumers with `kind: "stream"` and fixed execution identity.
Use `managed_identity_grants` on `fdp_workload` to grant its registered identity
Reader on its topic and only the required destination permissions. A stream has
readiness, capacity accounting, deployment history and lifecycle; it has no public
app URL or on-demand Run endpoint. For custom protocols call `fdp.streaming.ready()`
after initialization; Kafka SDK factories announce readiness automatically.
An initialization failure must exit the process so deployment health reports it.

Acknowledged produces and non-empty consumes report observed lineage. In workload
context these become `program → topic` and `topic → program` edges. Topics appear
under their cluster in the catalog's expandable hierarchy. Topic freshness is based
on SDK observations, throttled to at most one per client per 30 seconds; it is not
yet an authoritative broker message-age metric for arbitrary native producers.

## Rotation, recovery and validation

For broker policy-key rotation, retain the previous platform signing key in the
existing previous-keys setting, roll the control-plane/controller pods to read
the new installation Secret, rotate each layer's policy Secret through your
secret-management workflow, and verify access before dropping the former key.
Create-only reconciliation intentionally does not overwrite retained Secrets.
The broker re-reads its mounted credential per decision. Native client JWT signing
keys follow the existing Trino/JWKS key management procedure.

The isolated integration test runs an actual Kafka JVM with TLS, Strimzi's JWT
validator, the FDP authorizer, the Python SDK and the test control plane. It covers
producing, consumption, offset commits, group hijacking, wrong-topic writes,
forged signatures and revocation. It does not replace a Kubernetes acceptance
test of the operator, network enforcement, storage recovery or multi-broker failure.

```sh
cd images/kafka/authorizer
mvn -B -Pintegration verify dependency:build-classpath -Dmdep.outputFile=/tmp/kafka-classpath
cd ../../..
FDP_KAFKA_TEST_CLASSPATH=/tmp/kafka-classpath \
  python -m pytest services/control-plane/tests/test_kafka_broker.py -q
```

Install the control-plane dev dependencies and `sdk/python[kafka]` in the test
environment first. Java 21 is required. The CI pipeline performs this check and
blocks image publication if it fails.

Connect/Debezium/Iceberg workers, Apicurio contracts, the HTTP bridge, external
clusters, native mTLS listeners, lag/quotas and KEDA remain the later proposal
phases. No Kafka feature has been deployed to the live installation yet.
