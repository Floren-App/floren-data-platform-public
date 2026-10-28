# Floren Data Platform's own data

The database connection is configurable through `FDP_DATABASE_URL`. Compose uses
the bundled PostgreSQL by default. Select Global Admin to browse **Storage →
fdp-metadata → public**, query live metadata, and make validated SQL changes in
its SQL tab. See [Metadata SQL](metadata-sql.md) for configuration and supported commands.

Floren Data Platform's application metadata is separate from the storage integrations registered in
Storage. A program's Postgres/Iceberg tables do not contain Floren Data Platform's IAM or configuration.

The current local Compose deployment stores its data in these Docker volumes:

| Volume | Container location | Contents |
| --- | --- | --- |
| `fdp_pgdata` | Postgres `/var/lib/postgresql/data` | The `fdp` database: users/SCIM directory, groups, bindings, IAM, layers, integrations, workspaces, workloads, run records, catalog, lineage, governance, SLAs and audit history |
| `fdp_openbao-data` | OpenBao `/openbao/file` | Credentials, service-account secrets, certificates and other secret values |
| `fdp_openbao-keys` | Initializer `/openbao/keys`; read-only in the control plane | Local OpenBao initialization/unseal material and access token |
| `fdp-data` | Control plane `/fdp-data` | Deployed run consoles/outputs, workload files/docs, environment caches and runtime state |
| `fdp_gitea-data` | Gitea `/data` | Hosted Git repositories and Gitea's own SQLite database/configuration |
| `fdp_git-cache` | Control plane `/var/lib/fdp/git` | Cached Git checkouts; the authoritative repository remains in Gitea/the connected Git provider |
| `fdp-home-ws-<workspace-id>` | Editor and its compute runs `/workspace` | Only the volume's `workspace/` subdirectory: private files, Git checkout/settings, SQL scripts and editor state |

CloudBeaver saves SQL scripts under `/workspace/sql` and its internal metadata under
`/workspace/.fdp/cloudbeaver`. Stopping/restarting an editor keeps its volume. Deleting
a workspace currently removes its registration/container but retains the files' Docker volume.
Different workspaces have separate volumes even if they have the same name or creator.

On Docker Desktop for macOS these named volumes live inside Docker's Linux VM/disk image,
not directly in the project checkout. `docker volume inspect fdp_pgdata` reports its
Linux mount path. Host configuration (Compose files, TLS certificates and local Pokemon
configuration) remains under `deploy/` in this checkout.

## Identity-provider persistence

The local Keycloak service currently has no persistent data volume/database configured.
Its mutable identity data lives in the container's writable storage: an ordinary restart
keeps it, but recreating/removing that container does not. The mounted realm JSON is a
bootstrap import, not a backup of account changes. Floren Data Platform's SCIM directory remains in
its own Postgres database. Persistent IdP storage needs a separate migration that preserves
the existing realm/accounts; mounting an empty directory would not preserve them.

## Workspace startup

Container creation is not editor readiness. Workspaces report `starting` until the editor
answers its readiness check (CloudBeaver's health status, code-server's health endpoint, or
RStudio's HTTP entry point). The readiness monitor checks every two seconds; the UI polls
and enables Open editor after readiness. Startup defaults to a five-minute timeout and can
be configured with `FDP_WORKSPACE_START_TIMEOUT_SECONDS`.

The gateway serves an authenticated loading page during startup and a clear message after
failure/suspension. Existing workspace links refresh into the editor when it becomes ready.
The SQL image initializes the mounted volume as root, then runs CloudBeaver and the SQL
proxy as uid 1000. Existing files are retained and their ownership is not recursively changed.

On Docker, each editor and the runs started from it use a private workspace network. Only
the platform control plane, gateway and Git integration bridge that network. Other workspaces
and workloads cannot call its editor directly and impersonate gateway-authenticated users.
Managed Kubernetes layers use their own namespaces and enforced network policies; Docker workspace networks are not equivalent to a Kubernetes layer isolation test.

CloudBeaver's URI prefix uses its documented
[`CLOUDBEAVER_ROOT_URI`](https://dbeaver.com/docs/cloudbeaver/Server-configuration/) setting;
SQL query access continues through Floren Data Platform's governed gateway and its workspace identity.

## Project subdirectory

New private volumes expose only their `workspace/` subdirectory at `/workspace`.
The root recovery folder is outside the editor/job mount. Existing nonempty
root-layout volumes require an explicit migration or reset; see [workspaces](workspaces.md).
