# Using the app

FDP runs in the browser or as a desktop app. Both use the same API and the same sign-in.

## Group and layer

The bottom of the sidebar shows your working context:

- **Group**: the app group you act as. Its access applies to everything you see and do, it owns
  the objects you create, and the lists show its objects (workspaces, workloads, storage and
  catalog). Your other memberships add nothing until you switch to them. See
  [active groups](active-groups.md).
- **Layer**: the layer you work in. Home, Workspaces, Workloads (folders included), Storage,
  Kafka, APIs and Usage show that layer (items without a layer show in every layer), and what
  you create lands in it. A note in each list counts what is in other layers. Production
  layers are marked "production" in the picker. Your choice is remembered for your account.

The line above the tabs repeats it, **Acting as** *group* · *layer*, so it stays in view on every page.

Search the catalog from anywhere with **Ctrl/Cmd+K**.

The active group also decides which parts of the app you get: its [app profile](app-profiles.md)
switches whole sections on or off, so some of the sections below may not be there for you.

## Activity bar

The activity bar on the left shows icons. Hold the pointer over it for a moment, or move
to it with the keyboard, and it slides out over the sidebar with the name of each section.
It groups the sections by what you do:

| Group | Section | What it is for |
|---|---|---|
| Overview | **Home** | Your workspaces, recent runs, recent deployments and followed Airflow pipelines |
| | **Catalog** | Data products to find and request, with their lineage; All objects for engineers ([catalog](catalog.md)) |
| | **Usage** | How full each compute pool is and what runs on it, in your layer; **All layers** shows every layer ([compute pools](compute.md)) |
| Build | **Workspaces** | Your private VS Code, RStudio and SQL editors; tabs for their runs and your Git credentials ([workspaces](workspaces.md)) |
| | **Workloads** | Folders of published programs, apps, dbt projects and Airflow deployments; each folder has Recent runs and Logs tabs; administrators manage the Git repositories here ([publishing](deploying.md), [logs](logging.md)) |
| | **Storage** | Storage connections (databases, buckets) and their schemas, with metadata and data previews; administrators add connections here and change them on each connection's Settings tab ([storage integrations](storage-integrations.md)) |
| | **Packages** | The platform's Python and R package repository ([packages](packages.md)) |
| Integration | **Kafka** | Managed Kafka per layer and its topics; shown once a cluster is available to you |
| | **APIs** | Internal and external REST/SOAP APIs ([API management](api-management.md)) |
| Bottom | **Access** | The daily work around access. **To do**: access requests, reviews, new members and access that needs a form, waiting for you, with counts. **Who**: people, groups and service identities, each with what they can reach. **Records**: the access register, audit log and query log. **Credentials**: secrets ([RBAC](rbac.md), [access policies](access-policies.md)) |
| | **Administration** | The rules, set up once in a while. **Platform**: layers, compute pools, runtimes, workspace templates, alerts. **Security**: app profiles, network allowlist. **Privacy**: classifications, access policies, compliance forms, review duties |

Each person sees only the parts they may use: a privacy officer, for example, sees their reviews and
the register under Access, and the privacy rules under Administration if they keep them.

Access is in two places only: what an **object** allows is on its **Permissions** tab, and what a
**person, group or service identity** can reach is on its page under **Access**.

The **Documentation** button in the activity bar opens the guide for the current section.

Status dots mean the same on every page: green is running or succeeded, blue is ready (runs on
demand), yellow is starting or waiting, red is failed, and gray is stopped or idle.

Opening an object opens it in a tab. New pages replace the single unpinned preview tab;
pin a tab to keep it open. Pinned tabs come back the next time you open the app on the same device, if you still
have access to them.

Every page has its own address, so reloading keeps you on it and **Back** and **Forward** move between pages.
**Copy link** above the tabs copies the address to share. A link grants nothing: whoever opens it sees the page
only if they may, and a page they may not see does not open.

Leaving a page with changes you have not saved (going to another page, closing its tab or switching group) asks
first: save and continue, discard the changes, or stay. Closing or reloading the window asks too.

Lists that wait on someone else follow up by themselves while the page is open: data products and access requests
while a request is waiting for a decision, and backups while one runs. They also refresh when you come back to the
window. A small **Updated** time shows when a list was last loaded; if an update fails, the last list stays with the
reason, and is not shown as current.

## Home

Home shows your latest workspaces, recently started and recently finished runs, and
recently deployed workloads. Its search box uses the same authorized suggestions as the
catalog. Press Enter to open the full catalog search.

You can follow up to six Airflow deployments that you may execute. Each card shows the
latest DAG runs and refreshes every thirty seconds while Home is open, and opens the
native Airflow grid. **Choose orchestrators** saves your selection, with an optional DAG
filter per deployment, in your browser. A saved selection never grants access: it is
filtered against your current permissions each time.
