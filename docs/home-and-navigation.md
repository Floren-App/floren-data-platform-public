# Using the app

FDP runs in the browser or as a desktop app. Both use the same API and the same sign-in.

## Top bar

The top bar shows your working context and stays visible on every page:

- **Group**: which app group creates and owns new objects, and which objects the
  Workloads, Storage and Catalog lists show. Your other memberships still authorize actions
  on existing objects. See [active groups](active-groups.md).
- **Layer**: the layer you work in. Lists show that layer's workspaces, workloads,
  storage, Kafka and APIs (items without a layer show in every layer), and what you create
  lands in it. A note in each list counts what is in other layers. In a production layer
  the top bar and status bar turn amber. Your choice is remembered for your account.
- **Search the catalog** (or **Ctrl/Cmd+K**) opens catalog search from anywhere.

## Activity bar

The activity bar groups the sections by what you do:

| Group | Section | What it is for |
|---|---|---|
| Overview | **Home** | Your workspaces, recent runs, recent deployments and followed Airflow pipelines |
| | **Catalog** | Search, governance, lineage, quality and SLAs ([catalog](catalog.md)) |
| | **Usage** | How full each compute pool is and what runs on it; **This layer / All layers** switches the scope ([compute pools](compute.md)) |
| Build | **Workspaces** | Your private VS Code, RStudio and SQL editors ([workspaces](workspaces.md)) |
| | **Workloads** | Folders of published programs, apps, dbt projects and Airflow deployments, plus run history ([publishing](deploying.md)) |
| | **Storage** | Registered databases, buckets and their schemas, with metadata and data previews ([storage integrations](storage-integrations.md)) |
| | **Packages** | The platform's Python and R package repository ([packages](packages.md)) |
| Integration | **Kafka** | Managed Kafka per layer and its topics; shown once a cluster is available to you |
| | **APIs** | Internal and external REST/SOAP APIs ([API management](api-management.md)) |
| Bottom | **Security** | App groups, users, service identities and secrets ([RBAC](rbac.md)) |
| | **Administration** | Platform settings, layers, compute pools, runtimes, repositories and integrations (administrators only) |

The **Documentation** button in the activity bar opens the guide for the current section.

Opening an object opens it in a tab. New pages replace the single unpinned preview tab;
pin a tab to keep it open.

## Home

Home shows your latest workspaces, recently started and recently finished runs, and
recently deployed workloads. Its search box uses the same authorized suggestions as the
catalog. Press Enter to open the full catalog search.

You can follow up to six Airflow deployments that you may execute. Each card shows the
latest DAG runs and refreshes every thirty seconds while Home is open, and opens the
native Airflow grid. **Choose orchestrators** saves your selection, with an optional DAG
filter per deployment, in your browser. A saved selection never grants access: it is
filtered against your current permissions each time.
