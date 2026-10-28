# Storage integrations

Floren Data Platform does not host data. It governs databases and buckets that are operated elsewhere,
and each one it governs is an explicit **storage integration**. Until an administrator adds
one, there is no storage in Floren Data Platform at all.

Add one under **Administration → Storage integrations → Add integration**. First choose the
type, then describe the location. The form asks for exactly what that type needs, and the
control plane validates the same rules (`GET /v1/admin/storage-types` serves them; see
`services/control-plane/app/storage_types.py`).

| Type | Location you configure | Credentials Floren Data Platform keeps (OpenBao, write-only) | Must exist outside Floren Data Platform |
|---|---|---|---|
| **PostgreSQL** | host, port, database, Trino catalog name; optional SSL mode | access-setup login (creates roles, grants schema privileges); optional native-executor login | a Trino catalog (`connector.name=postgresql`) for the same database, with the engine's own login |
| **Iceberg tables on S3** | Trino catalog name, warehouse (`s3://bucket/path`), REST catalog URL; optional S3 endpoint, region | none | an Iceberg REST catalog, the bucket, and a Trino catalog (`connector.name=iceberg`, REST type) with its own S3 keys |
| **S3 files** | bucket, region, role to assume; optional base prefix, S3 and STS endpoints | STS provisioning access key | the bucket and a role that the provisioning key may assume |

What happens after an integration is added:

- Owners register schemas or namespaces on it and assign roles under **Data**.
- SQL types run every query through the shared Trino engine, so table credentials stay
  in the engine.
- S3 files issue short-lived credentials scoped to the granted prefixes.
- An Iceberg warehouse bucket is reserved for tables. It cannot also be registered as
  S3 files, and an S3 files bucket cannot become a warehouse.

Once created, an integration's type is fixed. When you edit one, leave a credential empty to
keep the stored value. Deleting an integration removes only its registration and stored
credentials; the storage itself is not touched.

## Engine catalogs

The SQL types are queried through catalogs that you configure in the shared Trino engine, not
in Floren Data Platform:

- **Compose:** one `<catalog>.properties` file per integration in
  `deploy/compose/trino/catalog/`.
- **Helm:** entries in the `trino.catalogSecret` secret.

The catalog name is what you enter as **Trino catalog**. Restart Trino after adding a catalog.

## Local example storage

To have something to test with, the repository includes example external storage. It stands
in for what an organisation already runs, and lives in `deploy/compose/example-storage/`, apart
from the platform's `docker-compose.yml`:

- `warehouse`: a PostgreSQL database `finance`.
- `rustfs`: S3-compatible storage with bucket `lake` (Iceberg tables) and bucket `files` (plain files).
- `iceberg-rest`: an Iceberg REST catalog for `s3://lake/`.
- `trino-catalog/finance.properties` and `trino-catalog/lake.properties`: the engine side of
  the two SQL integrations. While the example storage is enabled, this directory replaces
  `deploy/compose/trino/catalog/`.

Example storage is opt-in. Start it with `EXAMPLE_STORAGE=1 POKEMON_STORAGE=0 make dev`.
The default `make dev` runs without this example storage (and preserves the Pokémon overlay
if that project has been installed).

It is connected through a setup action that takes the same two steps you would take for your
own storage:

```sh
make storage-example   # deploy/compose/example-storage/setup.sh
```

1. **Outside Floren Data Platform:** prepare the storage (here, it creates the warehouse schemas
   `analytics` and `dbt_ana`; the Compose services and catalog files already exist).
2. **In Floren Data Platform:** register three integrations through the admin API
   (`example-storage/register.py`):

   | Integration | Type | Location |
   |---|---|---|
   | `finance-dw` | PostgreSQL | `warehouse:5432/finance`, catalog `finance` |
   | `lake` | Iceberg tables on S3 | `s3://lake/` via `http://iceberg-rest:8181`, catalog `lake` |
   | `lake-files` | S3 files | `s3://files/finance` on `http://rustfs:9000`, scoped STS role |

`make seed` (or `EXAMPLE_STORAGE=0 bash deploy/compose/seed.sh`) only ensures that the local
admin/admin account is provisioned. It does not require these integrations or add demo
users, repositories, namespaces or grants. Existing objects and permissions are preserved.

To install the finance demo deliberately, start the example stack above, then run
`EXAMPLE_STORAGE=1 POKEMON_STORAGE=0 make seed`. This runs `storage-example` first and adds
example identities, namespaces and roles on top (`seed_rbac.py`). This demo seed is separate
from the Pokémon installation and should be used only for the disposable example stack.

## Bringing your own storage

1. Make the location reachable from the `data` network (or your cluster), and create its
   engine catalog for PostgreSQL or Iceberg.
2. Add the integration in the desktop app, or script it like `register.py` does:

   ```sh
   curl -X PUT "$GATEWAY/api/v1/admin/datasources/sales-dw" -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{
       "name": "sales-dw", "kind": "postgres",
       "config": {"host": "pg.internal", "port": 5432, "database": "sales", "catalog": "sales"},
       "provisioner_secret": {"username": "governance_admin", "password": "…"}}'
   ```

   An incomplete request is refused with per-field errors.
3. Under **Data**, register its schemas, set up access, and assign roles.

Integrations registered before types were enforced keep working. They are validated the next
time they are saved. Re-save an Iceberg integration with its warehouse location, so that its
bucket is reserved for tables.
`FDP_SQL_LAKE_BUCKETS` can reserve additional table buckets.
