import { useEffect, useRef, useState } from "react";
import { api, EDITOR_NAME, inGroup, inLayer, isActive, openInWindow, workloadDot, workloadStatus, type OrchestratorHistory, type OrchestratorRun, type Workload, type Run } from "../api";
import { getServer } from "../auth";
import type { Hub } from "../hub";
import type { TabRef } from "./Tabs";
import { PageHeader } from "../components/Page";
import { IconHome, IconOpen, IconPlus, IconDeployed, IconRefresh, IconRuns, IconWorkspaces } from "../components/Icons";
import { ProductIcon } from "../components/ProductIcon";
import { CatalogSearchBox } from "./Catalog";
import { openEditor } from "./Sidebar";
import { WorkspaceSections } from "./WorkspaceSections";

type OpenTab = (tab: TabRef) => void;
const stamp = (value?: string | null) => value ? new Date(value).getTime() : 0;
const time = (value?: string | null) => value ? new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const runTab = (run: Run): TabRef => ({ kind: "run", ref: run.id, title: run.program || run.entrypoint.split("/").pop() || run.id,
  runScope: run.workload_id ? "deployed" : "workspace" });
const historyTab: TabRef = { kind: "folder", ref: "workloads", title: "Workloads", section: "runs" };
/** Home and the Workspaces page follow the working context: the active group's workspaces in the layer mode. */
const myWorkspaces = (hub: Hub) => hub.workspaces.filter((w) => inLayer(hub.layerId, w.layer_id ?? w.environment_id) && inGroup(hub.me?.active_group_id, w.owner_group_id));
const workloadsTab: TabRef = { kind: "folder", ref: "workloads", title: "Workloads" };

function Status({ value }: { value: string }) {
  const dot = ({ success: "succeeded", queued: "pending", up_for_retry: "pending" } as Record<string, string>)[value] || value;
  return <span className="status"><span className={`dot ${dot}`} />{value.replaceAll("_", " ")}</span>;
}

function ActivityList({ runs, hub, openTab, updated = false }: { runs: Run[]; hub: Hub; openTab: OpenTab; updated?: boolean }) {
  return runs.length ? <ul className="home-list">{runs.map((run) => {
    const workload = hub.workloads.find((p) => p.id === run.workload_id);
    return <li key={run.id}><button className="home-list-link" onClick={() => openTab(runTab(run))}>
      <ProductIcon kind={run.kind} runtime={run.runtime || undefined} size={18} />
      <span className="home-item-text"><strong>{workload?.title || run.program || run.entrypoint}</strong><small>{workload ? `${workload.branch} · ` : ""}{run.pool_name} · {time(updated ? run.finished_at || run.created_at : run.created_at)}</small></span>
      <Status value={run.status} />
    </button></li>;
  })}</ul> : <p className="home-empty">No runs yet. Run a program from a workspace or a workload.</p>;
}

export function Home({ hub, openTab }: { hub: Hub; openTab: OpenTab }) {
  const [query, setQuery] = useState("");
  const workspaces = myWorkspaces(hub).sort((a, b) => stamp(b.last_activity_at) - stamp(a.last_activity_at));
  const runs = hub.runs.filter((r) => inLayer(hub.layerId, r.layer_id));
  const recent = [...runs].sort((a, b) => stamp(b.created_at) - stamp(a.created_at));
  const workloads = hub.workloads.filter((p) => inLayer(hub.layerId, p.layer_id));
  const products = [...workloads].sort((a, b) => stamp(b.deployed_at || b.created_at) - stamp(a.deployed_at || a.created_at));
  const search = () => openTab({ kind: "catalog", title: "Catalog", search: query.trim() });
  const newWorkspace = () => openTab({ kind: "workspaces", title: "Workspaces", intent: "create" });
  return <div className="home-dashboard">
    <div className="home-intro"><PageHeader icon={<IconHome size={22} />} title={hub.me ? `Welcome back, ${hub.me.username}` : "Welcome to Floren Data Platform"}
      subtitle="Pick up your work, follow your pipelines, and find what you need."
      actions={<><button className="btn secondary" onClick={() => void hub.refresh()}><IconRefresh size={14} />Refresh</button><button className="btn" onClick={newWorkspace}><IconPlus size={14} />New workspace</button></>} />
      <div className="home-search"><CatalogSearchBox id="home-search" label="Search across Floren Data Platform" autoFocus={false} value={query} onChange={setQuery} openTab={openTab} onSubmit={search} /><button className="btn secondary" onClick={search}>Search</button></div>
      <p className="hint">Find workloads, datasets, APIs, columns, owners, and tags in the catalog.</p>
    </div>
    <div className="home-stats" aria-label="Your activity at a glance">
      <button onClick={() => openTab({ kind: "workspaces", title: "Workspaces" })}><IconWorkspaces size={18} /><strong>{workspaces.filter((w) => w.status === "running").length}</strong><span>Workspaces running</span></button>
      <button onClick={() => openTab(historyTab)}><IconRuns size={18} /><strong>{runs.filter(isActive).length}</strong><span>Active in recent runs</span></button>
      <button onClick={() => openTab(workloadsTab)}><IconDeployed size={18} /><strong>{workloads.length}</strong><span>Available workloads</span></button>
      <button onClick={() => openTab(historyTab)}><span className="dot failed" /><strong>{runs.filter((r) => r.status === "failed").length}</strong><span>Failures in recent runs</span></button>
    </div>
    <div className="home-grid">
      <section className="home-card" aria-labelledby="home-workspaces"><div className="home-card-heading"><h2 id="home-workspaces"><IconWorkspaces size={18} />Your workspaces</h2><button className="text-button" onClick={() => openTab({ kind: "workspaces", title: "Workspaces" })}>View all</button></div>
        {workspaces.length ? <ul className="home-list">{workspaces.slice(0, 4).map((ws) => <li key={ws.id}>
          <button className="home-list-link" onClick={() => openTab({ kind: "workspace", ref: ws.id, title: ws.name })}><IconWorkspaces size={18} /><span className="home-item-text"><strong>{ws.name}</strong><small>{ws.owner} · {EDITOR_NAME[ws.editor]}{ws.environment_id ? ` · ${hub.layers.find((r) => r.id === (ws.layer_id || ws.environment_id))?.name || "Scoped"}` : ""}</small></span><Status value={ws.status} /></button>
          {ws.status === "running" && <button className="icon-btn" aria-label={`Open editor for ${ws.name}`} title="Open editor" onClick={() => void openEditor(hub, ws.id, ws.name)}><IconOpen size={16} /></button>}
        </li>)}</ul> : <div className="home-empty"><p>Your workspace is where you edit and explore.</p><button className="btn secondary" onClick={newWorkspace}>Create a workspace</button></div>}
      </section>
      <section className="home-card" aria-labelledby="home-recent"><div className="home-card-heading"><h2 id="home-recent"><IconRuns size={18} />Recent runs</h2><button className="text-button" onClick={() => openTab(historyTab)}>View all</button></div><ActivityList runs={recent.slice(0, 4)} hub={hub} openTab={openTab} /></section>
      {hub.me && <OrchestratorWatch key={`${getServer()}:${hub.me.username}`} hub={hub} openTab={openTab} />}
      <RecentUpdates hub={hub} openTab={openTab} />
      <section className="home-card" aria-labelledby="home-workloads"><div className="home-card-heading"><h2 id="home-workloads"><IconDeployed size={18} />Recently deployed</h2><button className="text-button" onClick={() => openTab(workloadsTab)}>View all</button></div>
        {products.length ? <ul className="home-list">{products.slice(0, 4).map((pub) => <li key={pub.id}><button className="home-list-link" onClick={() => openTab({ kind: "workload", ref: pub.id, title: pub.title })}>
          <ProductIcon kind={pub.kind} runtime={pub.runtime} size={18} /><span className="home-item-text"><strong>{pub.title}</strong><small>{pub.branch} · {time(pub.deployed_at || pub.created_at)}</small></span><span className={`status ${pub.sync_error ? "error" : "muted"}`}><span className={`dot ${pub.sync_error ? "failed" : workloadDot(pub)}`} />{pub.sync_error ? "Needs attention" : workloadStatus(pub)}</span>
        </button></li>)}</ul> : <p className="home-empty">Apps and programs will appear here when deployed.</p>}
      </section>
    </div>
  </div>;
}

function RecentUpdates({ hub, openTab }: { hub: Hub; openTab: OpenTab }) {
  const [runs, setRuns] = useState<Run[] | null>(null), [error, setError] = useState("");
  useEffect(() => {
    if (!hub.me) return;
    let alive = true;
    api.runs(4, "updated").then((rows) => { if (alive) { setRuns(rows); setError(""); } })
      .catch((e: Error) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [hub.runs, hub.me?.username]);
  return <section className="home-card" aria-labelledby="home-updated"><div className="home-card-heading"><h2 id="home-updated"><IconRefresh size={18} />Recently updated runs</h2></div><p className="hint">Latest starts and completions, including jobs started earlier.</p>
    {error && <p role="alert" className="error">{error}</p>}
    {runs ? <ActivityList runs={runs.filter((r) => inLayer(hub.layerId, r.layer_id))} hub={hub} openTab={openTab} updated /> : !error && <p className="home-empty" role="status">Loading run activity…</p>}
  </section>;
}

export function WorkspacesView({ hub, openTab }: { hub: Hub; openTab: OpenTab }) {
  const workspaces = myWorkspaces(hub), hidden = hub.workspaces.length - workspaces.length;
  return <><PageHeader icon={<IconWorkspaces size={22} />} title="Workspaces" subtitle="Your private development environments. Each workspace has private files and a layer boundary. Your object permissions still apply."
    actions={<button className="btn" onClick={() => openTab({ kind: "workspaces", title: "Workspaces", intent: "create" })}><IconPlus size={14} />New workspace</button>} />
    <WorkspaceSections active="workspaces" openTab={openTab} />
    <div className="table-scroll"><table aria-label="Your workspaces"><thead><tr><th>Workspace</th><th>Owner</th><th>Editor</th><th>Layer</th><th>Status</th><th /></tr></thead><tbody>
      {workspaces.map((ws) => <tr key={ws.id}><td><button className="text-button" onClick={() => openTab({ kind: "workspace", ref: ws.id, title: ws.name })}>{ws.name}</button></td><td>{ws.owner}</td><td>{EDITOR_NAME[ws.editor]}</td><td>{hub.layers.find((r) => r.id === (ws.layer_id || ws.environment_id))?.name || "—"}</td><td><Status value={ws.status} /></td><td>{ws.status === "running" && <button className="btn sm secondary" onClick={() => void openEditor(hub, ws.id, ws.name)}><IconOpen size={14} />Open editor</button>}</td></tr>)}
      {!workspaces.length && <tr><td colSpan={6} className="muted">{hidden ? "No workspaces of this group in this layer." : "No workspaces yet. Create one to start developing."}</td></tr>}
    </tbody></table></div>
    {!!hidden && <p className="muted">{hidden} more in other groups or layers. Switch at the bottom of the sidebar to see {hidden === 1 ? "it" : "them"}.</p>}</>;
}

type Watches = Record<string, string>;
function readWatches(key: string): Watches | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "null");
    return value && typeof value === "object" && !Array.isArray(value)
      ? Object.fromEntries(Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === "string").slice(0, 6)) : null;
  } catch { return null; }
}

function OrchestratorWatch({ hub, openTab }: { hub: Hub; openTab: OpenTab }) {
  const storageKey = `fdp.orchestrators:${getServer()}:${hub.me!.username}`;
  const [saved, setSaved] = useState<Watches | null>(() => readWatches(storageKey)), [editing, setEditing] = useState(false), [refresh, setRefresh] = useState(0);
  const available = hub.workloads.filter((p) => p.kind === "airflow" && p.actions.includes("execute"));
  const watches = saved ?? Object.fromEntries(available.slice(0, 3).map((p) => [p.id, ""]));
  const selected = available.filter((p) => Object.prototype.hasOwnProperty.call(watches, p.id));
  const save = (next: Watches) => { setSaved(next); try { localStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* Session changes still work when storage is disabled. */ } };
  return <section className="home-card home-orchestrators" aria-labelledby="home-orchestrators"><div className="home-card-heading"><h2 id="home-orchestrators"><ProductIcon kind="airflow" size={18} />Orchestrator runs</h2><div className="row"><button className="text-button" onClick={() => setRefresh((v) => v + 1)}>Refresh runs</button><button className="btn sm secondary" aria-expanded={editing} onClick={() => setEditing(!editing)}>Choose orchestrators</button></div></div>
    <p className="hint">Follow up to six deployments and filter to a specific DAG. Run status refreshes every 30 seconds while Home is open.</p>
    {editing && <fieldset className="home-watch-picker"><legend>Deployments to follow</legend>{available.map((pub) => <label className="row" key={pub.id}>
      <input type="checkbox" checked={Object.prototype.hasOwnProperty.call(watches, pub.id)} disabled={!Object.prototype.hasOwnProperty.call(watches, pub.id) && selected.length >= 6} onChange={(e) => { const next = { ...watches }; if (e.target.checked) next[pub.id] = ""; else delete next[pub.id]; save(next); }} />{pub.title}<span className="badge">{pub.branch}</span>
    </label>)}{!available.length && <p className="muted">No Airflow deployments you can open.</p>}</fieldset>}
    <div className="home-orchestrator-grid">{selected.map((pub) => <OrchestratorCard key={pub.id} pub={pub} dag={watches[pub.id]} refresh={refresh} hub={hub} openTab={openTab} onDag={(dag) => save({ ...watches, [pub.id]: dag })} />)}</div>
    {!selected.length && <p className="home-empty">{available.length ? "Choose an orchestrator to follow its DAG runs here." : "Deploy an Airflow deployment to follow pipeline runs here."}</p>}
  </section>;
}

function OrchestratorCard({ pub, dag, refresh, hub, openTab, onDag }: { pub: Workload; dag: string; refresh: number; hub: Hub; openTab: OpenTab; onDag: (dag: string) => void }) {
  const [history, setHistory] = useState<OrchestratorHistory | null>(null), [error, setError] = useState(""), [fetched, setFetched] = useState<string | null>(null);
  const generation = useRef(0);
  useEffect(() => {
    let alive = true;
    setHistory(null); setError(""); setFetched(null);
    const load = async () => {
      const current = ++generation.current;
      try { const result = await api.orchestratorRuns(pub.id, dag); if (alive && current === generation.current) { setHistory(result); setError(""); setFetched(new Date().toISOString()); } }
      catch (e) { if (alive && current === generation.current) setError((e as Error).message); }
    };
    void load(); const timer = setInterval(() => void load(), 30_000);
    return () => { alive = false; clearInterval(timer); };
  }, [pub.id, dag, refresh]);
  const open = (run?: OrchestratorRun) => hub.act(() => openInWindow(api.openWorkload(pub.id).then(({ url }) => {
    const target = new URL(url);
    if (run) { target.pathname += `dags/${encodeURIComponent(run.dag_id)}/grid`; target.searchParams.set("dag_run_id", run.run_id); }
    return target.toString();
  }), pub.title, `orchestrator-${pub.id}`));
  return <article className="home-orchestrator" aria-label={pub.title}><div className="home-card-heading"><div><button className="text-button home-strong" onClick={() => openTab({ kind: "workload", ref: pub.id, title: pub.title })}>{pub.title}</button><small className="muted">{pub.branch} · {workloadStatus(pub)}</small></div><button className="icon-btn" aria-label={`Open ${pub.title} in Airflow`} title="Open Airflow" onClick={() => void open()}><IconOpen size={16} /></button></div>
    <select aria-label={`DAG for ${pub.title}`} value={dag} onChange={(e) => onDag(e.target.value)}><option value="">All DAGs</option>{[...new Set([...(history?.dags || []), ...(dag ? [dag] : [])])].map((id) => <option key={id}>{id}</option>)}</select>
    {error ? <p role="alert" className="error">{error}{history && " Showing the last successful refresh."}</p> : !history && <p className="muted" role="status">Loading DAG runs…</p>}
    {history && <ul className="home-list">{history.runs.map((run) => <li key={`${run.dag_id}:${run.run_id}`}><button className="home-list-link" title={run.run_id} onClick={() => void open(run)}><span className="home-item-text"><strong>{run.dag_id}</strong><small>{time(run.end_date || run.start_date || run.logical_date)} · {run.run_id}</small></span><Status value={run.state} /></button></li>)}{!history.runs.length && <li className="home-empty">No DAG runs recorded{dag ? ` for ${dag}` : ""}.</li>}</ul>}
    {fetched && <small className="muted home-checked">Checked {time(fetched)}</small>}
  </article>;
}
