# Kafka as a workload type: plan

Status: accepted (2026-10-30) with the recommended decisions below; progress in `docs/kafka-implementation.md`.

## Goal

Streams become a first-class part of Floren Data Platform, governed exactly like tables: the platform owns
identity, authorization, lineage and configuration; Kafka enforces them. Nobody handles a Kafka password, and
integration (databases, the lake, external systems) is a supported, governed path rather than custom glue.

Principles, the same as for SQL:

- **Every producer and consumer is an identity** (a person, or a workload's managed identity).
- **Grants decide access**, on topics, through the existing roles and OpenTofu.
- **Credentials never reach user code**: programs get short-lived tokens from the platform.
- **Layers isolate**: a managed Kafka lives in its layer's namespace and network boundary.
- **Open source only**: Apache-2.0 components; no Confluent Community License software.

## Which Kafka

| | Managed per layer | Bring your own |
|---|---|---|
| What | Kafka (KRaft) in the layer namespace, run by **Strimzi** (Apache-2.0, CNCF) | An existing cluster registered as a storage integration, like PostgreSQL and S3 |
| Sizing | Brokers sized by a warehouse in the layer's infrastructure, like Trino workers | The owner's |
| Authentication | Platform-issued JWTs (OAUTHBEARER), validated against the platform's JWKS | Per-identity SCRAM users or OAuth, created by the platform with one admin credential |
| Authorization | A plugin asks the control plane for every decision (like Trino) | ACLs the platform keeps in sync with grants |
| Network | The layer's default-deny policies; only the layer's workloads and connectors | The owner's; the platform needs a route |

Start with **managed**: the platform controls authentication and authorization end to end, so the model is
proven before adapting it to clusters it does not control.

## Identity and access

- **Topics are governed resources** under their Kafka integration (resource kind `topic`), like schemas under a
  database. Roles map to Kafka operations:

  | Role | Allows |
  |---|---|
  | Reader | Consume (read, describe), commit offsets for the identity's own consumer groups |
  | Contributor | Also produce (write, idempotent write) |
  | Admin | Also create, alter and delete topics (partitions, retention, compaction) |
  | Owner | Also assign roles |

- **Consumer groups are owned by identities**: group names are prefixed with the identity
  (`<principal>.<name>`), so one consumer cannot read another's offsets or hijack its group.
- **Authentication**: the SDK fetches a short-lived token for the program's identity (as for SQL), and the
  Kafka client sends it with OAUTHBEARER. Strimzi's OAuth library validates it against the platform's JWKS,
  the keys Trino already trusts. No secrets are stored in Kafka or in programs.
- **Authorization**: `opa-kafka-plugin` (Apache-2.0) calls a control plane policy endpoint, the same pattern as
  Trino's access control: one rule set, decisions audited and exported to the SIEM, unknown operations denied.
  The plugin caches decisions briefly; a revoked grant takes effect within that cache time.
- **Quotas** per identity (bytes per second), set alongside warehouses, so one producer cannot starve a layer.

## Integration

This is where Kafka earns its place, so each path is governed end to end.

### 1. Programs in the platform

- `fdp.kafka.producer("orders")` and `fdp.kafka.consumer("orders", group="billing")` return fully configured
  clients (bootstrap servers, TLS, token refresh). R gets the same through its SDK.
- A **stream** workload type: long-running consumers deployed like apps, from Git, with GitOps updates,
  warehouses, layers and managed identities. Optional lag-based autoscaling with KEDA (Apache-2.0).
- Airflow and dbt can trigger on or materialize from streams through the Iceberg sink (below).

### 2. Connectors (Kafka Connect, managed as workloads)

| Connector | License | Use |
|---|---|---|
| Debezium (PostgreSQL, others) | Apache-2.0 | Change data capture from registered database integrations into topics |
| Apache Iceberg Kafka Connect sink | Apache-2.0 | Land topics as governed Iceberg tables in the layer's lake |
| S3 and JDBC sinks (open-source) | Apache-2.0 | Files and databases as destinations |
| HTTP source | Apache-2.0 | Poll registered APIs through API management |

- Connectors reference **registered integrations** (a PostgreSQL source, the layer lake); the platform supplies
  credentials through a config provider, so connector configuration never contains a secret.
- Each connector runs as its own managed identity with grants like any workload, and appears in lineage
  (`postgres.orders` → topic `orders` → `lake.reporting.orders`).
- Only allowlisted connector plugins can run (an OSI-licensed list), so the supply chain stays reviewable.

### 3. External systems

- **HTTP, through API management**: Strimzi's Kafka Bridge (Apache-2.0) behind the API gateway. A partner
  produces or consumes over HTTPS with the API's JWT or mTLS authentication and a Consumer role, quotas and
  audit, exactly like other internal APIs. The easiest path for most integrations.
- **Native Kafka, optional per layer**: a TLS listener with mTLS client certificates for systems that speak
  Kafka, routed by SNI through the gateway. Off by default.
- **Bring-your-own clusters** (phase 3) cover organizations that already run Kafka.

### 4. Schemas and data contracts

- **Apicurio Registry** (Apache-2.0) per layer, not Confluent Schema Registry (Confluent Community License).
  Topics declare a schema (Avro, JSON Schema or Protobuf) and a compatibility rule; producers that break it are
  refused.
- The catalog shows each topic's schema, owners, SLA and lineage; schemas feed the semantic layer (Ossie) later.

### 5. Observability and SLAs

- Consumer lag, throughput and message age per topic and group in the compute overview.
- Freshness SLAs on topics (time since the last message), like dataset SLAs.
- Broker health in the layer infrastructure status.

### 6. Configuration as code

| OpenTofu | What |
|---|---|
| `fdp_layer` `infrastructure.kafka` | Brokers (warehouse, count), storage, listeners |
| `fdp_kafka_topic` | Partitions, retention, compaction, schema and compatibility |
| `fdp_role_assignment` | Grants on topics (existing resource) |
| `fdp_kafka_connector` | Connector plugin, integrations it uses, its options |

## Governance limits

- Access is per topic. Field-level masking inside messages is not enforced by Kafka; publish a separate topic
  (a connector or stream program that drops fields) for consumers who may not see everything.
- Data copied out of Kafka (sinks) is governed at its destination (table grants, masks and row filters).

## Phases

| Phase | Contents | Proves |
|---|---|---|
| 1 | Managed Kafka per layer (Strimzi, KRaft); topics as governed resources; platform tokens; authorization through the control plane; SDK; stream workloads; catalog and lineage | The core model |
| 2 | Kafka Connect with Debezium and the Iceberg sink; Apicurio schemas and compatibility | Integration: database → stream → lake |
| 3 | HTTP bridge through API management; optional native listener; bring-your-own clusters with ACL sync | External integration |
| 4 | Lag-based autoscaling, topic SLAs, quotas in the compute overview | Operations |

Each phase ships with tests, OpenTofu resources, app pages, documentation and a live check on the Pokémon
example (for example: card price changes as a stream, landed into the lake by the Iceberg sink).

## Decisions needed before phase 1

1. **Managed first or bring-your-own first?** Recommended: managed.
2. **Strimzi or own manifests?** Recommended: Strimzi (certificates, rolling upgrades, topic and user resources).
3. **Authorization: plugin asking the control plane, or ACL sync?** Recommended: the plugin for managed Kafka
   (one source of truth, audit and lineage for free); ACL sync for bring-your-own clusters.
4. **Schema registry: Apicurio or Karapace** (both Apache-2.0)? Recommended: Apicurio (more formats and rules).
