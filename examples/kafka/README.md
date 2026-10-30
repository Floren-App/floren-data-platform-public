# Pokémon streaming example

This is a separate, portable example for the managed Kafka phase. It has not been
copied into or deployed over the private Pokémon installation. TCGDex supplies
card metadata here, not a made-up card-price feed.

`pokemon-publish` is a scheduled/on-demand script. Through a registered TCGDex
external API it reads two small English set inventories, preserving card IDs and
assigning deterministic content event IDs. It publishes through its managed
identity. `pokemon-stream-landing` is a continuous consumer with a different
managed identity. It writes a JSON array per batch into a governed S3 namespace
and commits offsets only after the write succeeds. No per-record S3 files.

Replay can overlap batch boundaries. Downstream ingestion must deduplicate by
Kafka topic/partition/offset; `event_id` identifies content and `record_id` follows
the card. A Kafka commit and an S3 upload are not one atomic transaction. Invalid
events stop the consumer without committing rather than disappearing silently.

Copy these source files into a dedicated repository with `test` and `main`
branches. Its `fdp.json` is independent of the existing batch/dbt/dashboard example.
Use the module in `terraform/` with provider authentication from your normal
OpenTofu configuration. It requires already configured per-layer Kafka clusters,
TCGDex APIs allowing `GET /sets/{id}`, repositories, folders, workload warehouses
and landing namespaces. Publish the FDP Kafka SDK into the platform package
repository before deploying programs that require it.

Supply exactly two instances: `test` uses branch `test`; `prod` uses branch `main`.
Their layers, clusters, API resources and landing schemas must differ. Use three
replicas for Prod topics. The server also rejects cross-layer managed-identity
grants, so a Test workspace cannot borrow these Prod identities. Give engineers
the required layer-scoped groups; analysts get no stream/landing grants. Existing
curated products can remain their governed entry point.

The module grants producer Consumer on its own API and Contributor on its topic;
the landing identity gets only Reader on its own topic and Contributor on its
own storage namespace. Existing API/S3 and Kafka SDK instrumentation records
observed lineage; no manually declared lineage JSON is necessary.

Connect, Debezium, Iceberg sinks and registry contracts are not implemented by
these scripts; they remain separate proposal phases. See `docs/kafka-implementation.md`.
