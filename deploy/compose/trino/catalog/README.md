# Trino catalogs for your storage

One `<catalog>.properties` file per storage integration, holding the engine's own connection
and credentials (these never reach Floren Data Platform or workloads). Register the matching integration
under **Administration → Storage integrations** with the same catalog name.

The local example storage (`deploy/compose/example-storage`) replaces this directory with its
own catalogs while it is enabled. See `docs/storage-integrations.md`.
