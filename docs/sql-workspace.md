# SQL workspace

A SQL workspace is a query editor for the shared engine. It covers what people use DataGrip or
Snowflake worksheets for:

- a schema browser of every catalog, schema, table and column you can access;
- a SQL editor with completion;
- result grids;
- saved `.sql` scripts.

It is [CloudBeaver Community](https://github.com/dbeaver/cloudbeaver) (Apache-2.0), the web
edition of DBeaver, run unchanged as a third workspace editor next to VS Code and RStudio.
Create one in the desktop app under **Workspaces → + → Editor: SQL**.

## How it connects

```
browser ──gateway (sign-in, path cookie)──▶ CloudBeaver ──JDBC──▶ local proxy ──workspace token──▶ SQL gateway ──▶ Trino
```

- **Sign-in:** the gateway authenticates every request and passes the user to CloudBeaver
  through its reverse-proxy sign-in (`X-FDP-User`). There is no second login.
  CloudBeaver uses a stable `dc-<directory-id>` account to avoid collisions between human
  names such as `admin` and CloudBeaver's built-in team names. Its display name remains
  the workspace owner's username. The gateway clears client-supplied proxy identity/team
  headers and supplies a regular user team; Floren Data Platform IAM still governs SQL access.
- **Connection:** CloudBeaver has one managed connection, **Floren Data Platform SQL**. It uses
  Trino's JDBC driver against a small proxy inside the container
  (`images/workspace-sql/fdp-sql-proxy.py`). The proxy adds this workspace's token
  and forwards only the Trino client protocol.
- **Credentials:** no token or password is stored in CloudBeaver.
- **Governance:** every statement goes through the same SQL gateway as programs. Schema
  roles, policy checks, row filters and the audit log are unchanged, and queries are
  recorded against this workspace.
- **Browsing:** the schema tree reads Trino's `system.jdbc` tables. Trino builds those rows
  through Floren Data Platform's access-control filters, so the tree lists only objects you have a
  data role on.

## Where scripts live

Saved scripts are plain files in `/workspace/sql` on this workspace's private persistent
volume. Other workspaces have separate volumes. Commit scripts to Git when you want to
use them in another workspace or project. CloudBeaver keeps its own settings in
`/workspace/sql/.dbeaver` and `/workspace/.fdp/cloudbeaver`.

## Startup

Creating the container reports **starting** until CloudBeaver's health endpoint confirms
it is ready. The workspace page updates automatically and enables **Open editor** when
startup completes. Existing editor links show an authenticated loading page while waiting.
If startup fails or times out, the workspace displays a failure message and **Retry start**.
Files remain on the volume across retries. See [platform storage](platform-storage.md).

## Keeping data on the server

As in the other editors, data does not leave the server:

- Result export, data import and AI chat are switched off in the image.
- The gateway also refuses CloudBeaver's file-transfer endpoints (`api/data/`,
  `api/resultset/blob/`, `api/fs-data/`) for SQL workspaces.
- Adding other connections is hidden. A workspace reaches data only through the governed
  gateway, like every other workspace.

Downloading and uploading the *text* of a script stays possible, the same as copying code.

## Limits

- **Autocommit only:** statements run in autocommit mode, because the gateway does not
  support explicit transactions, so the manual-commit switch is hidden.
- **Token lifetime:** the workspace token is fixed when the workspace starts and lasts
  8 hours. Workspaces suspend after 30 minutes idle, and starting again issues a new token.

Build the image with `make images` (or `docker build -f images/workspace-sql/Dockerfile -t
fdp/workspace-sql:dev .`). The CloudBeaver version is pinned in that Dockerfile.
