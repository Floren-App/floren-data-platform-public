# Platform metadata database and SQL

Floren Data Platform's operational database is configured with `FDP_DATABASE_URL`: your
own PostgreSQL, or the optional bundled one. It is separate from your business data and
from the secret store's values.

Select **Global Admin** and open **Storage → fdp-metadata → public → SQL**.
Application tables are also indexed in Catalog: identities, groups, bindings, permissions,
workloads, workspaces, runs, storage registrations, APIs, governance, lineage and audit.
Tables have the existing column browser and 25-row preview.

```sql
SELECT id, name, description FROM fdp_metadata.public.app_groups;

SELECT r.name AS resource, g.name AS app_group, a.role
FROM fdp_metadata.public.role_assignments a
JOIN fdp_metadata.public.rbac_resources r ON r.id = a.resource_id
JOIN fdp_metadata.public.app_groups g ON g.id = a.app_group_id;
```

Reads run in Trino against the live database. Filter and limit large operational tables.
This sensitive catalog is not delegable to other groups, services, jobs or workspaces.
An administrator's ordinary workspace keeps its layer restrictions. Use the schema SQL
tab or the Trino gateway with an interactive OIDC token, rather than an execution token.

## Validated changes

Both `/v1/statement` (Trino clients) and `/v1/sql/query` (UI) accept management SQL when
Global Admin is active. Commands use the UI's existing validation and audit trail. Trino
has a separate SELECT-only database account. Direct writes, table/schema changes and
native SQL pass-through remain denied.

```sql
INSERT INTO fdp_metadata.public.app_groups (name, description)
VALUES ('Finance Readers', 'Read finance reporting');
-- The current administrator is the initial designated owner.

UPDATE fdp_metadata.public.app_groups
SET description = 'Finance reporting readers' WHERE id = 'group-id';

INSERT INTO fdp_metadata.public.identity_bindings (app_group_id, principal_id)
VALUES ('group-id', 'idp-group-or-user-id');

GRANT Reader ON RESOURCE 'schema-resource-id' TO GROUP 'Finance Readers';
REVOKE Reader ON RESOURCE 'schema-resource-id' FROM GROUP 'Finance Readers';

INSERT INTO fdp_metadata.public.service_role_assignments
  (resource_id, principal_id, role)
VALUES ('schema-resource-id', 'service-principal-id', 'Contributor');

INSERT INTO fdp_metadata.public.role_assignments
  (resource_id, app_group_id, role)
VALUES ('schema-resource-id', 'group-id', 'Reader');

DELETE FROM fdp_metadata.public.identity_bindings
WHERE app_group_id = 'group-id' AND principal_id = 'idp-group-or-user-id';
```

`GRANT/REVOKE role ON RESOURCE 'id' TO/FROM GROUP|SERVICE|USER 'recipient'` is Floren Data Platform
gateway syntax. GROUP accepts an ID or exact group name; SERVICE and USER accept directory
principal IDs. Built-in roles apply; this does not create native Trino roles.
Assignment INSERT uses the same upsert behavior as the UI. Direct service grants retain
same-layer validation.

| Table | Supported changes |
| --- | --- |
| `app_groups` | INSERT; UPDATE name/description/owners; DELETE when no objects/grants remain |
| `identity_bindings` | INSERT and DELETE |
| `role_assignments` | INSERT, UPDATE role, DELETE |
| `service_role_assignments` | INSERT, UPDATE role, DELETE |
| `catalog_assets` | UPDATE one governance or SLA JSON document with UI policy validation |

Use a JSON string or `ARRAY['principal-id']` for owners. Governance/SLA JSON uses the HTTP
API's fields. All other metadata is read-only, including IdP facts, audit events, token
records, runtime state, layer IDs and execution snapshots. Lifecycle APIs still create
workspaces, storage and deployments. Protected Global Admin and IdP administrator bindings
cannot be removed. Revocations apply to new admissions; running job snapshots remain.

Management SQL accepts one literal row per statement. UPDATE/DELETE must select exactly
one existing row by ID or complete assignment/binding keys. No computed assignments,
subqueries, bulk updates or explicit transactions. Prepared statements and EXECUTE
IMMEDIATE with literal parameters are supported.

## Database configuration

To relocate metadata, provision PostgreSQL, migrate the existing database, change
`FDP_DATABASE_URL`, and restart Floren Data Platform. Update the metadata JDBC URL to the same
database and ensure Trino can reach it. Changing a URL does not migrate existing data.

Local development (Compose) defaults:

| Setting | Default |
| --- | --- |
| `FDP_DATABASE_URL` | `postgresql+psycopg://fdp:fdp@postgres:5432/fdp` |
| `FDP_METADATA_JDBC_URL` | `jdbc:postgresql://postgres:5432/fdp` |
| `FDP_METADATA_SQL_ENABLED` | `true` |
| `FDP_METADATA_READER_USER` | `fdp_metadata_reader` |
| `FDP_METADATA_READER_PASSWORD` | Development credential; replace outside local development |
| `FDP_METADATA_BOOTSTRAP_READER` | `true` |

Startup provisions a dedicated reader with schema USAGE and table SELECT. Database owner
credentials never reach Trino. Set bootstrap false to provision this reader yourself;
refresh its SELECT grants after migrations. Metadata tables use the `public` schema.
SQLite is available for API tests and development only; this SQL catalog requires PostgreSQL.

Helm exposes `metadataSql.enabled`, `.catalog`, `.readerUser`, `.bootstrapReader` and
`.existingSecret`. The secret has keys `metadata-jdbc-url` and `metadata-reader-password`;
the control-plane secret holds `FDP_DATABASE_URL`. Production defaults to a reader
provisioned by your DBA. Bootstrap requires role-management privileges and a reader name
beginning with `fdp_metadata_`. A custom catalog name must match both Trino and
`FDP_METADATA_CATALOG`; in Compose also rename the catalog properties file.

Compose connects PostgreSQL and Trino over a dedicated internal metadata network.
Warehouse backends and workload containers do not join that network.
