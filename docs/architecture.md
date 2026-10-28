> Shared SQL execution now runs through Trino for PostgreSQL and Iceberg: [implementation and deployment](query-execution.md). Earlier direct database/embedded lake flows below are historical.

> Current authorization implementation and connector coverage: [RBAC](rbac.md). The original whole-source grants and deployer-identity flows below are superseded by that model.

# Floren Data Platform — Architecture

Status: draft v0.1 · Owner: platform team · Last updated: 2026-10-16

## 1. Why

Analysts need Python, Jupyter, Streamlit, R and Shiny. Their load swings from "editing a script" to
"training on 30 GB", and data must never land on their laptops. Floren Data Platform gives them one desktop
hub that connects to server-side workspaces and on-demand compute, with admins controlling exactly
which data each group can reach.

**Principles**

1. **The device is a window, not a workstation.** No code execution, files or credentials on the laptop.
2. **Pay for compute only while it works.** Editing is tiny and cheap; heavy work runs on sized pools that
   start on demand and suspend when idle (the Snowflake warehouse model).
3. **Data access is granted, not configured.** Admins register sources and grant them to groups;
   analysts call `connect("name")` and never see a password.
4. **Free and open source only.** Every component is OSI-licensed (see §9).

## 2. Overview

```
 Analyst laptop                         Server (Kubernetes)
┌──────────────────┐   HTTPS   ┌───────────────────────────────────────────────────────────┐
│ Floren Data Platform app   │──────────▶│ Gateway (Traefik)                                          │
│ (Tauri)          │           │   /api/v1/*  ──▶ Control plane (FastAPI + Postgres)        │
│  hub UI          │           │   /ws/<id>/* ──▶ Workspace pod   (code-server | RStudio)   │
│  editor windows ─┼──────────▶│   /apps/<id>/*─▶ App pod         (Streamlit | Shiny)       │
└──────────────────┘           │        ▲ forwardAuth on every request                      │
        │ OIDC (PKCE)          │                                                            │
        ▼                      │ Control plane ──▶ Kubernetes API (pods, PVCs, policies)    │
 Keycloak / Entra ID / Okta    │               ──▶ OpenBao (data source secrets)            │
                               │ Workloads ──(session token)──▶ /v1/connect/<source>       │
                               │ Workloads ──(NetworkPolicy)──▶ granted data sources only   │
                               └───────────────────────────────────────────────────────────┘
```

## 3. Components

| Component | Tech | Responsibility |
|---|---|---|
| Desktop hub | Tauri 2 + React | Sign-in, list/start/stop workspaces, launch runs and apps, admin screens. VS Code-style layout. Opens editors in their own windows. |
| Gateway | Traefik v3 | Single public entry point. Routes come from the control plane (HTTP provider); every editor/app request passes `forwardAuth`. |
| Control plane | Python, FastAPI, SQLAlchemy, Postgres | Users/groups (from the IdP), data catalog and grants, compute pools, workspaces, runs, audit log, auto-suspend. |
| Orchestrator | Kubernetes API (Docker Engine for local dev) | Creates pods, services, per-user volumes and egress policies. |
| Secrets | OpenBao | Data source credentials (KV v2), optionally dynamic short-lived DB users. |
| Editor images | code-server, RStudio Server OSS | The VS Code and RStudio experiences, served from the server. |
| Compute images | python:3.12, rocker/r-ver | Scripts, notebooks (papermill), Streamlit and Shiny apps. |
| SDKs | `fdp` (Python + CLI), `fdp` (R) | `connect()`, `run()`, `sources()` from inside workspaces and runs. |

## 4. Compute model: workspaces and pools

| Tier | Snowflake analogy | Runs | Default size | Lifecycle |
|---|---|---|---|---|
| **Workspace** | Snowsight | Editor, git, terminal, light exploration | 0.5 vCPU · 1 GB | Suspends after 30 min without gateway traffic |
| **Compute pool** | Virtual warehouse | Scripts, notebooks, apps | Sizes defined by administrators or OpenTofu (none by default; GPU optional) | Script/notebook: until done. App: suspends after the pool's idle timeout |

- A run is started from the hub (Runs panel / workspace tab) or from a workspace terminal
  (`fdp run train.py --pool large`, `fdp::run_on("large", "train.R")`).
- Runs mount the same per-user volume at `/workspace`, so "edit small, run big" needs no copying.
  On Kubernetes this needs a **ReadWriteMany** storage class (NFS, CephFS, Longhorn).
- Pods request exactly their pool size. The **cluster autoscaler** adds nodes when pools are busy
  and removes them when they suspend — this is what absorbs variable load.
- Pools can be limited to groups (e.g. only `data-science` may use `gpu-large`) and capped by
  `max_concurrent`: every container that uses the warehouse's compute takes one slot (a job, a deployed app,
  each Airflow component). Work beyond the limit waits in line (status `queued`, with its position) and the
  control plane starts it in arrival order when a slot frees; new work never overtakes the line. A queued job
  stores only secret *paths*, never values, and reads them when it starts. A deploy that needs more slots
  than are free waits too, while the running version keeps serving; updates and restarts of a running
  workload need no extra slot and never wait.
- Each layer's Trino has a coordinator of its own size and workers sized by one warehouse chosen for the
  layer (`trino.workers`). Every running worker takes one slot on that warehouse and the minimum always
  runs. Queries share the workers and do not take slots, so the configuration states Trino's compute:
  coordinator size, worker warehouse, minimum and maximum workers. The platform scales the workers between
  minimum and maximum with running and waiting queries; a worker that is removed finishes its queries first.
- A **dedicated** warehouse belongs to a team (`dedicated = true` with `allowed_groups`, created only by
  administrators), for work that must start whenever it is needed, such as a business-critical Airflow. It is
  not one of the shared on-demand warehouses: it has no slot limit, its work never waits in line and counts
  toward nothing else. Only its groups run or deploy on it. Administrators make sure the cluster has nodes for
  the dedicated warehouses they create. Trino workers and workspaces use shared warehouses only.
- A **default warehouse** (`fdp_default_warehouse`, or **Make default** on the pool admin page) is where work
  without a warehouse runs, and what warehouse choices start with. A program whose `fdp.json` names a warehouse
  that does not exist runs on the default instead of failing; an explicit choice must exist.
- A warehouse that runs a layer's Trino workers is **Trino's alone**: jobs, apps, Airflow and workspaces cannot
  use it, it is not offered in warehouse choices, and it cannot be the default. A layer's Trino workers need a
  warehouse that nothing else uses.
- A run or editor the cluster cannot place yet stays **pending with the reason** (for example "waiting for
  cluster capacity: no node has enough memory free", or an image that cannot be pulled); an image that can
  never be pulled fails the run at once.
- Runs of deployed programs and runs started in workspaces are listed apart (Workloads and Workspaces).
  Workspace runs are private to the workspace's owner and administrators.
- **Workspace templates** give groups defaults: an administrator (Administration → Workspace templates, or
  `fdp_workspace_template`) defines a workspace for an app group, and every member gets their own private
  workspace from it, created stopped, including people who join later. A workspace always has one user.
  Leaving the group removes that person's workspace (its files stay in its volume); a workspace the person
  deleted is not created again.
- **Package repository** (Packages): one repository with Python and R packages and every version, stored on
  the control plane's `fdp-packages` volume. pip/uv read a simple index (`/v1/packages/python/simple/`),
  R a CRAN-style repository (`/v1/packages/r`); executions get `PIP_INDEX_URL`, `UV_DEFAULT_INDEX`, `FDP_CRAN`
  and `RENV_CONFIG_REPOS_OVERRIDE` with a short read-only package key in the path (`/x/<key>/`), valid only
  while their workspace, run or workload is active (never the token: installers print addresses, and R
  names cache files after them), and twine/uv publish settings that send their token as the password.
  Everyone reads; publishing follows the `packages` resource's roles (Publisher, Maintainer, Owner), and a
  published package's group owns its new versions. Publishers import from PyPI (wheels only, via
  `pip download` for every platform Python version and CPU type) and CRAN (source, dependencies resolved
  from PACKAGES); imports run one at a time in the control plane, the only component that reaches them.
- **Query log**: every query on a layer's Trino is recorded. The coordinator's HTTP event listener posts each
  completed query to the control plane with the layer's events token (`fdp-trino-events`, generated by the
  layer controller); the gateway's admission id travels as Trino's trace token, so each row names the person or
  service identity and the run, workload or workspace, with tables read and written, duration, CPU, rows, bytes
  and errors. SQL text only when administrators turn it on (`fdp_query_log_settings.record_sql`); rows past
  `retention_days` (default 90) are removed by the leader. Administration → Query log; people see their own.
- **Metrics**: `GET /internal/metrics` (Prometheus, bearer `FDP_METRICS_TOKEN`; Helm
  `metrics.serviceMonitor.enabled` for the Prometheus Operator). Every replica reports its HTTP requests
  (per route template, with durations), whether it leads and the queries it recorded; the leader also reports
  workspaces, active runs, warehouse slots and queues, workloads, package imports, the last hour's queries per
  layer and the certificate checks.
- **Replicas**: the control plane runs as several replicas (Helm `replicas`) behind one Service. Each serves
  requests; one leader, holding a PostgreSQL advisory lock on the metadata database, runs the background work
  (Git sync, reaper, editor readiness, SLA checks, Trino scaling, catalogs, certificate checks, package
  imports). If it stops or loses its database connection, the lock is released and another replica takes over
  within about half a minute. Deploys hold a cluster-wide lock per workload. Replicas share the package volume
  (ReadWriteMany) and are spread across nodes, with a disruption budget.
- **Package allowlist**: optionally only listed public packages may be imported (app, API, `fdp_package`);
  dependencies of an allowed package come along, and published packages are not affected
  (`fdp_package_allowlist`, Packages → Allowed packages).
- **Network allowlist**: VS Code and RStudio reach the internet only through the egress proxy
  (`fdp-egress-proxy`, Helm `egressProxy.enabled`), which tunnels HTTPS to the host names an administrator
  allows (Administration → Network allowlist, or `fdp_egress_allowlist`) and refuses names that resolve to
  private addresses. Editors get the standard proxy variables and the `fdp.floren.app/egress: proxy` label;
  network policies let only those pods reach the proxy, and the proxy only public port 443. The proxy reloads
  the list every few seconds. Other executions and the SQL editor have no internet access.
- **Git** uses people's own tokens: each person keeps one or more credentials (Account → Git credentials; e.g.
  work and personal), each a host, username, token in the secret store, and the name/email to commit as. The
  first start of a VS Code or RStudio workspace asks which one it uses (or none); that choice can change on
  the workspace's page. git asks for the token through `git-credential-fdp` with the workspace's execution
  token; only that person's own running editor gets it, never runs or deployed programs, and nothing is
  written to the volume. The commit name and email are set in the workspace's Git config at start. A template's repositories are cloned at the workspace's first start. The platform
  itself only reads from Git (deployments fetch with the administrators' read-only repository credentials).
- Workspaces never take warehouse slots. They have their own limits, set by an administrator (desktop admin
  page or `fdp_workspace_settings`): a total of running workspaces (more wait in line), a per-person limit
  (running and waiting workspaces count; beyond it a start is refused), and one warehouse whose size every
  editor gets.
- The Compute tab shows each warehouse's slots in use, waiting work and use per layer. Everyone sees the
  counts; only work you may already see (your runs and workspaces, workloads you can browse) is listed.

## 5. Data boundary

What keeps data on the server:

1. **No local execution.** The desktop app has no filesystem or shell permissions (Tauri capabilities).
2. **Editors cannot export files.** code-server runs with `--disable-file-downloads --disable-file-uploads`;
   RStudio with `allow-file-downloads=0`, `allow-file-uploads=0`.
3. **Network egress is default-deny.** Execution pods can reach only DNS and the control plane.
   Registering a data source with egress CIDRs creates a NetworkPolicy that opens that source **only**
   for pods labelled `fdp.floren.app/ds-<name>=true` — and pods only get that label when the owner's
   groups are granted the source at start time.
4. **Ingress only via the gateway**, and every request is authenticated and owner-checked.
5. **Audit log** of every connect (and denial), run, workspace start and catalog change.

Known residual risks (accepted for v0.1, see §10): screen capture / copy-paste of rendered output,
and the Docker backend does not enforce egress (local development only).

## 6. Secrets

- Admins submit credentials once (`PUT /v1/admin/datasources/<name>` with `secret`); the control plane
  writes them to OpenBao at `fdp/datasources/<name>` and never returns them.
- Every workload gets a **session token** (signed JWT: user, groups, workspace, 8 h TTL) in its
  environment. The SDK trades it at `/v1/connect/<name>` for connection details; the control plane
  checks the grant, audits, and reads the secret from OpenBao at that moment.
- With `config.dynamic_role` set, OpenBao's database secrets engine issues a **fresh database user per
  connect** with a lease — no shared long-lived passwords at all. Recommended for production.
- Credentials are never written to disk by the SDK and never shown in the hub UI.

## 7. Identity

- Any OIDC provider. The desktop app discovers issuer and client id from `GET /api/v1/client-config`,
  so users only type the server URL.
- The desktop is a **public client** using authorization code + PKCE (no client secret in the app).
- Claims are configurable: `roles` path (Keycloak `realm_access.roles`, Entra ID `roles`), `groups`,
  username. The admin role name is configurable (`fdp-admin` by default).
- Keycloak with a test realm (users `admin`, `ana`, `bob`) ships with the local stack.

## 8. Key flows

**Open an editor.** Hub → `POST /v1/workspaces/<id>/open` → one-time 60 s ticket URL → editor window
loads `/ws/<id>/?dc_ticket=…` → gateway forwardAuth validates the ticket and swaps it for an HttpOnly
cookie scoped to `/ws/<id>/` → later requests are checked against that cookie and the workspace owner.
Separate windows (not iframes) keep cookies first-party in the Tauri webview.

**Connect to data.** `fdp.connect("finance-dw")` → `GET /v1/connect/finance-dw` with the session
token → grant check → OpenBao read (or dynamic credential) → SQLAlchemy engine / DBI connection.
The connection is allowed through the network only because the pod has the source's label.

**Run on a pool.** `POST /v1/runs {workspace, pool, kind, entrypoint}` → path validated (must stay inside
`/workspace`) → pod with the pool's CPU/memory → status and logs polled; the reaper records the result.

## 9. Licensing (all free / open source)

| Component | License |
|---|---|
| code-server | MIT |
| RStudio Server (open-source edition) | AGPL-3.0 |
| Jupyter, papermill | BSD-3 |
| Streamlit | Apache-2.0 |
| Shiny (R) | MIT |
| Keycloak | Apache-2.0 |
| OpenBao (instead of HashiCorp Vault, which is BSL) | MPL-2.0 |
| Traefik | MIT |
| Kubernetes / k3s | Apache-2.0 |
| PostgreSQL | PostgreSQL License |
| Tauri | MIT / Apache-2.0 |
| FastAPI, SQLAlchemy | MIT |

Not used: Posit Workbench, Shiny Server Pro, Docker Desktop, the Microsoft VS Code Marketplace
(code-server uses Open VSX; Pylance is replaced by basedpyright).

## 10. Roadmap

**Phase 1 — this scaffold:** catalog + grants, OpenBao secrets, workspaces (VS Code, RStudio), runs on
pools (script, notebook, Streamlit, Shiny), gateway auth, auto-suspend, audit, Helm chart, local stack.

**Phase 2**
- **Remote notebook kernels:** Jupyter Enterprise Gateway so a notebook *edited* in the tiny workspace
  runs its kernel on a chosen pool (true "Snowsight + warehouse" for interactive work).
- **App sharing:** deploy a Streamlit/Shiny app to groups with a stable URL.
- Per-group quotas; usage/cost dashboard.
- **Production desktop sign-in** via the system browser + loopback redirect; signed auto-updates.
- **Package mirrors** (devpi for Python, a local CRAN mirror for R) so workloads need no internet.
- Alembic migrations; multiple control-plane replicas with a leader-elected reaper.

**Hardening**
- Rootless RStudio image so the runtime namespace can enforce the `restricted` Pod Security level.
- Clipboard/download policy review, watermarking for sensitive sources.
- Rotate the session signing key via OpenBao; shorter token TTL with refresh.

## 11. Open questions

1. Which ReadWriteMany storage is available on the target cluster?
2. Production IdP and group naming (who owns group membership)?
3. Which data sources come first, and can they use dynamic credentials?
4. Is copy/paste of query results out of the editor acceptable, or must it be blocked?
