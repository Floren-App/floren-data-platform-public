# Using the app

FDP runs in the browser or as a desktop app. Both use the same API and the same sign-in.

## Activity bar

| Section | What it is for |
|---|---|
| **Home** | Your workspaces, recent runs, recent deployments and followed Airflow pipelines |
| **Workspaces** | Your private VS Code, RStudio and SQL editors ([workspaces](workspaces.md)) |
| **Workloads** | Folders of published programs, apps, dbt projects and Airflow deployments, plus run history ([publishing](deploying.md)) |
| **Storage** | Registered databases, buckets and their schemas, with metadata and data previews ([storage integrations](storage-integrations.md)) |
| **APIs** | Internal and external REST/SOAP APIs ([API management](api-management.md)) |
| **Packages** | The platform's Python and R package repository ([packages](packages.md)) |
| **Catalog** | Search, governance, lineage, quality and SLAs ([catalog](catalog.md)) |
| **Security** | App groups, users, service identities and secrets ([RBAC](rbac.md)) |
| **Compute pools** | Execution sizes, their limits and current activity ([compute pools](compute.md)) |
| **Administration** | Platform settings, layers, runtimes, repositories and integrations (administrators only) |

The **Documentation** button in the activity bar opens the guide for the current section.

Opening an object opens it in a tab. New pages replace the single unpinned preview tab;
pin a tab to keep it open. **Ctrl/Cmd+K** opens catalog search from anywhere.

## Active group

The **Active group** selector at the bottom of every sidebar decides which app group
creates and owns new objects, and which objects the Workloads, Storage and Catalog lists
show. Your other memberships still authorize actions on existing objects. See
[active groups](active-groups.md).

## Home

Home shows your latest workspaces, recently started and recently finished runs, and
recently deployed workloads. Its search box uses the same authorized suggestions as the
catalog. Press Enter to open the full catalog search.

You can follow up to six Airflow deployments that you may execute. Each card shows the
latest DAG runs and refreshes every thirty seconds while Home is open, and opens the
native Airflow grid. **Choose orchestrators** saves your selection, with an optional DAG
filter per deployment, in your browser. A saved selection never grants access: it is
filtered against your current permissions each time.
