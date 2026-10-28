# Home and navigation

The activity bar is ordered **Home → Workspaces → Workloads → Storage → APIs →
Catalog → Security → Compute pools**. Administration and Account remain in the
separate bottom section. Workloads has an application-grid icon; APIs keep the globe.
Opening an object or switching editor tabs selects the matching activity section.
New pages replace the single unpinned preview tab. Use the pin icon in a tab to keep
it open. Unpinning makes that page replaceable again; pinned pages can still be closed.
The bottom of every sidebar contains the persistent **Active group** selector. It controls
creation authority and the owning group of new objects; secondary memberships continue
authorizing existing-object access. See [active groups](active-groups.md).

Home is the opening editor. It shows four workspaces, four recent runs, four recently
updated runs, and four recently deployed workloads. Updated runs sort by completion
time, falling back to creation time; this represents recorded starts and completions.
This panel asks for its own four rows with `GET /v1/runs?limit=4&sort=updated`, so jobs
started long ago that finish now still appear. Other run cards and counts use the currently
loaded, authorized run history, initially twenty rows. They do not imply a platform-wide job inventory. Workspaces retain their existing
private identity and layer scope. Home search reuses the catalog's authorized
suggestions and passes the query to the full catalog on Enter or Search. Ctrl/Cmd+K
still opens catalog search directly.

The Workloads sidebar contains the folder/workload hierarchy followed by recent
runs. Its **Recent runs** link and the root's **Recent runs** tab open the same history
page, with search, status filters and pagination. Workspace/pool/object consoles and
rerun controls stay on their existing pages. There is no separate Runs activity.

## Orchestrator watch list

Home follows up to six Airflow deployments. It initially selects the first three
deployments the user can execute. **Choose orchestrators** saves an explicit selection;
each deployment also has an optional DAG filter. Preferences live in local storage,
scoped by server and username. Every render filters saved IDs against current workload
permissions. Clearing all selections is preserved. This preference never grants access.

Only while Home is mounted, selected deployments poll their three most recently updated
DAG runs every thirty seconds. Each card reports its last successful refresh, handles
unavailable history independently, and opens the native Airflow DAG grid with a newly
authorized gateway ticket. Deployment runtime status is separate from DAG-run status.

`GET /v1/workloads/{id}/orchestrator-runs?dag_id=&limit=5` checks workload visibility
and execute permission before opening metadata. Limits are 1–25. Standalone history uses
the workload's own `airflow/{id}/airflow.db` with SQLite `mode=ro`. Distributed history
uses its existing deployment database/login from OpenBao, with read-only transactions,
a connection timeout and statement timeout. The endpoint never provisions a database,
runs a DAG, executes user code, or queries data products. It returns only DAG/run IDs,
state and timestamps; run configuration, task arguments, credentials and driver errors
are excluded. Missing metadata yields a generic 503 rather than a misleading empty list.
