import { useEffect, useState } from "react";
import { accessibleSchemas, accessibleSources, ago, api, AccessResource, inGroup, shortSha, inLayer, poolDot, poolInLayer, rEnabled, EDITOR_LABEL, EDITOR_NAME, Editor, openInWindow, Run, RunKind, RunScope, startingPool, usablePools, workloadDot, workloadStatus } from "../api";
import { ProductIcon } from "../components/ProductIcon";
import { resourceTab } from "../components/Permissions";
import { SchemaTreeNode } from "../components/DataBrowser";
import { useCached, useObjectCache } from "../cache";
import type { Hub } from "../hub";
import { IconHome, IconWorkspaces, IconDeployed, IconCatalog, IconRuns, IconApi, IconBranch, IconChevron, IconClose, IconOpen, IconPlay, IconPlus, IconRefresh, IconStop, IconPackage, IconImport, IconKey, IconLog } from "../components/Icons";
import { MenuButton } from "../components/Menu";
import { SECURITY_PAGES, SecurityIcon } from "./Directory";
import type { TabRef } from "./Tabs";
import { ensureGitChoice } from "./GitCredentials";

export type View = "home" | "apis" | "catalog" | "access" | "workspaces" | "workloads" | "data" | "kafka" | "compute" | "packages" | "admin";

interface Props {
  view: View;
  hub: Hub;
  openTab: (t: TabRef) => void;
  activeKey: string;
  activeRef?: string;
  activeIntent?: TabRef["intent"];
}

export function Sidebar(props: Props) {
  const { view, hub } = props;
  const cache = useObjectCache();
  const title = { home: "Home", apis: "API management", catalog: "Catalog", access: "Security", workspaces: "Workspaces", workloads: "Workloads", data: "Storage", kafka: "Kafka", compute: "Usage", packages: "Packages", admin: "Administration" }[view];
  return (
    <>
      <h2>
        {title}
        <button className="icon-btn" title="Refresh" aria-label="Refresh" onClick={() => { cache.invalidate("data:"); void hub.refresh(); }}><IconRefresh /></button>
      </h2>
      {hub.error && (
        <div className="error row" role="alert" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <span>{hub.error}</span>
          <button className="icon-btn" aria-label="Dismiss error" title="Dismiss" onClick={hub.dismissError}><IconClose /></button>
        </div>
      )}
      {view === "access" && <SecurityPanel {...props} />}
      {view === "workspaces" && <WorkspacesPanel {...props} />}
      {view === "home" && <><div className="section-title">Your platform</div><ul className="list">
        {([{ kind: "home", title: "Overview", icon: <IconHome size={16} /> }, { kind: "workspaces", title: "My workspaces", icon: <IconWorkspaces size={16} /> },
          { kind: "folder", ref: "workloads", title: "Browse workloads", icon: <IconDeployed size={16} /> }, { kind: "runs", title: "Recent runs", icon: <IconRuns size={16} /> },
          { kind: "catalog", title: "Browse catalog", icon: <IconCatalog size={16} /> }] as (TabRef & { icon: JSX.Element })[]).map(({ icon, ...tab }) =>
          <li key={tab.kind} className={tab.kind === "home" ? "selected" : ""}><button className="nav-item" onClick={() => props.openTab({ ...tab, title: tab.kind === "home" ? "Home" : tab.kind === "folder" ? "Workloads" : tab.kind === "catalog" ? "Catalog" : tab.kind === "workspaces" ? "Workspaces" : tab.title })}>{icon}{tab.title}</button></li>)}
      </ul><p className="sidebar-note muted">Your work, recent activity, and the pipelines you follow. Search the catalog with ⌘K / Ctrl+K.</p></>}
      {view === "workloads" && <DeployedPanel {...props} />}
      {view === "catalog" && <>
        <div className="section-title">Browse catalog</div><ul className="list">{[["", "All objects"], ["table", "Tables"], ["source", "Storage connections"], ["schema", "Schemas"], ["app", "Apps"], ["script", "Programs"], ["dbt", "dbt projects"], ["airflow", "Airflow deployments"], ["folder", "Workload folders"]].map(([kind, label]) =>
          <li key={kind} className={props.activeKey === "catalog:" && (props.activeRef ?? "") === kind ? "selected" : ""}>
            <button className="nav-item" onClick={() => props.openTab({ kind: "catalog", ref: kind || undefined, title: "Catalog" })}><ProductIcon kind={kind === "app" ? "streamlit" : kind === "script" ? "python" : kind} size={16} />{label}</button></li>)}</ul>
        <p className="sidebar-note muted">Open a catalog object to explore lineage, governance, SLAs, or access. Results follow your current roles.</p>
      </>}
      {view === "data" && <DataPanel {...props} />}
      {view === "kafka" && <KafkaPanel {...props} />}
      {view === "apis" && <ApisPanel {...props} />}
      {view === "compute" && <ComputePanel {...props} />}
      {view === "packages" && <PackagesPanel {...props} />}
      {view === "admin" && <AdminPanel {...props} />}
    </>
  );
}


/** "3 more in other layers": items the layer (and group) mode hides from this list. */
function OtherLayers({ count, groups = false }: { count: number; groups?: boolean }) {
  return count ? <p className="sidebar-note muted">{count} more in other {groups ? "groups or layers" : "layers"}. Switch at the bottom of the sidebar to see {count === 1 ? "it" : "them"}.</p> : null;
}

export async function openEditor(hub: Hub, id: string, name: string) {
  await hub.act(() => openInWindow(api.openWorkspace(id).then((r) => r.url), `${name} — Floren Data Platform`, `ws-${id}`));
}

function WorkspacesPanel({ hub, openTab, activeKey, activeIntent }: Props) {
  const [adding, setAdding] = useState(false);
  useEffect(() => { if (activeKey === "workspaces:" && activeIntent === "create") { setAdding(true); openTab({ kind: "workspaces", title: "Workspaces" }); } }, [activeKey, activeIntent, openTab]);
  const [name, setName] = useState("");
  const [editor, setEditor] = useState<Editor>("vscode");
  const environments = hub.layers.filter((layer) => layer.workspaces);
  const [environment, setEnvironment] = useState("");
  const selectedEnvironment = environment || environments.find((l) => l.id === hub.layerId)?.id || environments[0]?.id || "";
  const shown = hub.workspaces.filter((w) => inLayer(hub.layerId, w.layer_id ?? w.environment_id) && inGroup(hub.me?.active_group_id, w.owner_group_id));

  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    await hub.act(async () => {
      const ws = await api.createWorkspace(name.trim(), editor, selectedEnvironment || undefined);
      // It exists now: close the form so a retry can't create a duplicate, even if starting
      // fails (e.g. the running-workspace limit). The error stays visible above the list.
      setAdding(false);
      setName("");
      if (await ensureGitChoice(ws)) await api.startWorkspace(ws.id);   // cancelled: created, not started
      openTab({kind:"workspace",ref:ws.id,title:ws.name});
    });
    setBusy(false);
  };

  return (
    <>
      <div className="section-title">
        My workspaces
        <button className="icon-btn" title="New workspace" aria-label="New workspace" aria-expanded={adding} onClick={() => setAdding(!adding)}><IconPlus /></button>
      </div>
      {adding && (
        <form className="form" onSubmit={(e) => { e.preventDefault(); void create(); }}>
          <div className="field">
            <label htmlFor="ws-name">Name</label>
            <input id="ws-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="churn-analysis" />
          </div>
          <div className="field">
            <label htmlFor="ws-editor">Editor</label>
            <select id="ws-editor" value={editor} onChange={(e) => setEditor(e.target.value as Editor)}>
              <option value="vscode">{EDITOR_LABEL.vscode}</option>
              {rEnabled(hub.me) && <option value="rstudio">{EDITOR_LABEL.rstudio}</option>}
              <option value="sql">{EDITOR_LABEL.sql}</option>
            </select>
          </div>
          {!!environments.length && <div className="field"><label htmlFor="ws-environment">Layer</label>
            <select id="ws-environment" value={selectedEnvironment} onChange={(e) => setEnvironment(e.target.value)}>
              {environments.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select><small>Access requires this layer and your own object permissions. No workspace security group is created.</small></div>}
          <div className="form-actions">
            <button className="btn sm" disabled={!name.trim() || busy || !selectedEnvironment}>{busy ? "Starting…" : "Create & start"}</button>
            <button type="button" className="btn sm secondary" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </form>
      )}
      <ul className="list">
        {shown.map((w) => (
          <li key={w.id} className={activeKey === `workspace:${w.id}` ? "selected" : ""}>
            <button className="nav-item" title={`${w.name} · ${w.owner} · ${w.status}${w.status === "running" ? " · double-click to open the editor" : ""}`}
                    onClick={() => openTab({ kind: "workspace", ref: w.id, title: w.name })}
                    onDoubleClick={() => w.status === "running" && openEditor(hub, w.id, w.name)}>
              <span className={`dot ${w.status}`} />
              <span className="grow">{w.name} <span className="sub">{w.owner} · {EDITOR_NAME[w.editor] ?? w.editor}{w.template_id ? " · group template" : ""}</span></span>
            </button>
            <span className="actions">
              {w.status === "running" || w.status === "starting" || w.status === "queued" ? (
                <>
                  <button className="icon-btn" disabled={w.status !== "running"} title={w.status === "queued" ? "Waiting for a free workspace slot" : w.status === "starting" ? "Editor is starting" : "Open editor"} aria-label="Open editor" onClick={() => openEditor(hub, w.id, w.name)}><IconOpen /></button>
                  <button className="icon-btn" title="Stop" aria-label="Stop" onClick={() => hub.act(() => api.stopWorkspace(w.id))}><IconStop /></button>
                </>
              ) : (
                <button className="icon-btn" title="Start" aria-label="Start" onClick={() => void ensureGitChoice(w).then((go) => go && hub.act(() => api.startWorkspace(w.id)))}><IconPlay /></button>
              )}
            </span>
          </li>
        ))}
        {!shown.length && !adding && <li className="empty"><button className="text-button" onClick={() => setAdding(true)}>{hub.workspaces.length ? "Create a workspace in this layer…" : "Create your first workspace…"}</button></li>}
      </ul>
      <OtherLayers count={hub.workspaces.length - shown.length} groups />
      <div className="sidebar-section-divider" />
      <ul className="list">
        <li className={activeKey === "runs:workspace" ? "selected" : ""}><button className="nav-item" onClick={() => openTab({ kind: "runs", ref: "workspace", title: "Workspace runs" })}><IconRuns size={16} />Workspace runs</button></li>
        <li className={activeKey === "git-credentials:" ? "selected" : ""}><button className="nav-item" onClick={() => openTab({ kind: "git-credentials", title: "Git credentials" })}><IconKey size={16} />Git credentials</button></li>
      </ul>
    </>
  );
}

export function NewRunForm({ hub, workspaceId, onDone }: { hub: Hub; workspaceId?: string; onDone?: () => void }) {
  const [ws, setWs] = useState(workspaceId ?? hub.workspaces[0]?.id ?? "");
  const [entrypoint, setEntrypoint] = useState("");
  const [kind, setKind] = useState<RunKind>("script");
  const [pool, setPool] = useState(startingPool(hub.me?.pools, undefined, hub.workspaces.find((w) => w.id === (workspaceId ?? hub.workspaces[0]?.id))?.layer_id, "script"));
  const layerId = hub.workspaces.find((w) => w.id === ws)?.layer_id;

  const submit = async () => {
    if (await hub.act(() => api.createRun(ws, pool, kind, entrypoint.trim()))) {
      setEntrypoint("");
      onDone?.();
    }
  };

  return (
    <form className={workspaceId ? "" : "form"} onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      {!workspaceId && (
        <div className="field">
          <label htmlFor="run-ws">Workspace (files)</label>
          <select id="run-ws" value={ws} onChange={(e) => setWs(e.target.value)}>
            {hub.workspaces.map((w) => <option key={w.id} value={w.id}>{w.name} · {w.owner}</option>)}
          </select>
        </div>
      )}
      <div className="field">
        <label htmlFor="run-kind">Type</label>
        <select id="run-kind" value={kind} onChange={(e) => setKind(e.target.value as RunKind)}>
          <option value="script">Script (.py / .R)</option>
          <option value="notebook">Notebook (.ipynb)</option>
          <option value="app">App (Streamlit .py / Shiny dir or app.R)</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="run-entry">File, relative to the workspace</label>
        <input id="run-entry" className="mono" value={entrypoint} onChange={(e) => setEntrypoint(e.target.value)}
               placeholder={kind === "app" ? "dashboards/sales.py" : "models/train.py"} />
      </div>
      <div className="field">
        <label htmlFor="run-pool">Warehouse</label>
        <select id="run-pool" value={pool} onChange={(e) => setPool(e.target.value)}>
          {!pool && <option value="">— choose —</option>}
          {usablePools(hub.me?.pools, layerId, kind).map((p) => <option key={p.id} value={p.name}>{p.name} · {p.cpu} vCPU · {p.memory_gb} GB{p.gpu ? ` · ${p.gpu} GPU` : ""}{p.default ? " (default)" : ""}</option>)}
        </select>
      </div>
      <div className="form-actions">
        <button className={`btn ${workspaceId ? "" : "sm"}`} disabled={!ws || !entrypoint.trim() || !pool}><IconPlay size={14} />Run</button>
        {onDone && <button type="button" className="btn sm secondary" onClick={onDone}>Cancel</button>}
      </div>
    </form>
  );
}

/** Runs of deployed programs (Workloads) or runs started in workspaces (Workspaces), never mixed. */
export const runsIn = (runs: Run[], scope: RunScope) => runs.filter((r) => scope === "deployed" ? !!r.workload_id : !r.workload_id);
export const RUN_SCOPE_TITLE: Record<RunScope, string> = { deployed: "Recent runs", workspace: "Workspace runs" };

/** Loads the next page of older runs; shared by the sidebar and run tables. */
export function LoadMoreRuns({ hub, className = "text-button" }: { hub: Hub; className?: string }) {
  const [busy, setBusy] = useState(false);
  return <button className={className} disabled={busy} onClick={async () => { setBusy(true); try { await hub.loadMoreRuns(); } finally { setBusy(false); } }}>
    {busy ? "Loading…" : "Load more runs"}</button>;
}

function DeployedPanel({ hub, openTab, activeKey }: Props) {
  const resources = hub.resources;
  const folder = (parent: string): JSX.Element => <ul className="object-tree">
    {resources.filter((r) => r.parent_id === parent && r.kind === "folder" && inLayer(hub.layerId, r.layer_id)).sort((a, b) => a.name.localeCompare(b.name)).map((r) => <li key={r.id}>
      <details><summary className={activeKey === `folder:${r.id}` ? "selected" : ""}>
        <span className="deployed-folder-heading"><button className="tree-label tree-name" onClick={(e) => { e.preventDefault(); e.currentTarget.closest("details")!.open = true; openTab(resourceTab(r)); }}><ProductIcon kind="folder" size={16} />{r.name}</button>
          <span className="row">
            {r.actions.includes("create") && <button className="icon-btn" aria-label={`New folder in ${r.name}`} title={`New folder in ${r.name}`} onClick={(e) => { e.preventDefault(); e.currentTarget.closest("details")!.open = true; openTab({ ...resourceTab(r), intent: "create-folder" }); }}><IconPlus size={14} /></button>}
            {r.actions.includes("read") && <button className="icon-btn" aria-label={`Permissions for folder ${r.name}`} title={`Permissions for ${r.name}`} onClick={(e) => { e.preventDefault(); openTab(resourceTab(r, "permissions")); }}><ProductIcon kind="identity" size={14} /></button>}
          </span>
        </span>
      </summary>{folder(r.id)}</details>
    </li>)}
    {hub.workloads.filter((p) => p.folder_id === parent && inLayer(hub.layerId, p.layer_id)).map((p) => <li key={p.id}>
      <button className={`tree-leaf ${activeKey === `workload:${p.id}` ? "selected" : ""}`} title={`${p.title} · ${workloadStatus(p)}${p.deployed_sha ? ` · version ${shortSha(p.deployed_sha)}, updated ${ago(p.deployed_at)}` : ""}`}
        onClick={() => openTab({ kind: "workload", ref: p.id, title: p.title })}>
        <span className={`dot ${workloadDot(p)}`} /><ProductIcon kind={p.kind} runtime={p.runtime} size={16} />
        <span className="tree-name">{p.title}</span><span className="muted">{p.kind === "script" || p.kind === "notebook" ? "on demand" : p.kind}</span>
        {p.sync_error && <span className="error" title={p.sync_error} aria-label="Sync error">⚠</span>}
      </button>
    </li>)}
  </ul>;
  const root = resources.find((r) => r.id === "workloads");
  return <>
    {root && <div className="section-title">
      <button className="section-link" onClick={() => openTab(resourceTab(root))}>All workloads</button>
      {resources.some((r) => (r.kind === "folder" || r.kind === "root") && r.actions.includes("create")) &&
        <MenuButton id="deployed-create-menu" label="Add workload" menuLabel="Create in Workloads" icon={<IconPlus />}>
          {(close) => <>
            <button role="menuitem" disabled={!root.actions.includes("create")} onClick={() => { close(); openTab({ ...resourceTab(root), intent: "create-folder" }); }}><ProductIcon kind="folder" size={16} />Folder</button>
            <button role="menuitem" onClick={() => { close(); openTab({ kind: "deploy", title: "Deploy" }); }}><IconBranch />Deploy</button>
          </>}
        </MenuButton>}
    </div>}
    {folder("workloads")}
    {!resources.some((r) => r.parent_id === "workloads") && <p className="sidebar-note muted">No workloads available.</p>}
    <OtherLayers count={hub.workloads.filter((p) => !inLayer(hub.layerId, p.layer_id)).length} />
    <div className="sidebar-section-divider" />
    <ul className="list"><li className={activeKey === "runs:" || activeKey === "runs:deployed" ? "selected" : ""}>
      <button className="nav-item" onClick={() => openTab({ kind: "folder", ref: "workloads", title: "Workloads", section: "runs" })}><IconRuns size={16} />Recent runs</button></li>
      <li><button className="nav-item" onClick={() => openTab({ kind: "folder", ref: "workloads", title: "Workloads", section: "logs" })}><IconLog size={16} />Logs</button></li></ul>
  </>;
}

function DataPanel({ hub, openTab, activeKey }: Props) {
  const [search, setSearch] = useState("");
  const all = accessibleSources(hub.resources);
  const sources = all.filter((s) => inLayer(hub.layerId, s.layer_id));
  const match = (name: string) => name.toLowerCase().includes(search.toLowerCase());
  return <>
    <div className="sidebar-search"><input type="search" aria-label="Search data sources and schemas" placeholder="Find a source or schema…" value={search} onChange={(e) => setSearch(e.target.value)} /></div>
    <ul className="object-tree">
      {sources.filter((s) => match(s.name) || accessibleSchemas(hub.resources, s.id).some((r) => match(r.name))).map((s) =>
        <SourceTreeNode key={s.id} source={s} hub={hub} search={search} activeKey={activeKey} openTab={openTab} />)}
    </ul>
    {!all.length && <p className="sidebar-note muted">No data sources you can access. Ask a data source owner for access.</p>}
    <OtherLayers count={all.length - sources.length} />
  </>;
}

function KafkaPanel({ hub, openTab, activeKey }: Props) {
  const all = hub.resources.filter((r) => r.kind === "kafka");
  const clusters = all.filter((c) => inLayer(hub.layerId, c.layer_id ?? String(c.config.layer_id ?? "")));
  return <>
    <div className="section-title"><button className="section-link" onClick={() => openTab({ kind: "kafka-clusters", title: "Kafka" })}>All clusters</button></div>
    <ul className="object-tree">{clusters.map((c) => <KafkaTreeNode key={c.id} cluster={c} activeKey={activeKey} openTab={openTab} />)}</ul>
    {!all.length && <p className="sidebar-note muted">No Kafka clusters you can access. Ask a cluster owner for access.</p>}
    {!!all.length && !clusters.length && <p className="sidebar-note muted">No Kafka in this layer.</p>}
    <OtherLayers count={all.length - clusters.length} />
  </>;
}

/** A cluster with its topics, fetched when it is first expanded (a cluster can hold many topics). */
function KafkaTreeNode({ cluster, activeKey, openTab }: { cluster: AccessResource; activeKey: string; openTab: Props["openTab"] }) {
  const [open, setOpen] = useState(false);
  const [topics, setTopics] = useState<AccessResource[]>();
  const [error, setError] = useState("");
  const layer = String(cluster.layer_id ?? cluster.config.layer_id ?? "");
  const load = () => { setError(""); api.kafka.topics(layer).then(setTopics).catch((e: Error) => setError(e.message)); };
  useEffect(() => { if (open && !topics) load(); }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  return <li><div className={`tree-node ${activeKey === `kafka:${cluster.id}` ? "selected" : ""}`}>
    <button className="tree-expander" aria-label={`${open ? "Collapse" : "Expand"} Kafka cluster ${cluster.name}`} aria-expanded={open} onClick={() => setOpen(!open)}><IconChevron /></button>
    <button className="tree-label tree-name" onClick={() => { setOpen(true); openTab(resourceTab(cluster)); }}><ProductIcon kind="kafka" size={16} />{cluster.name}</button>
    <span className="muted tree-type">{cluster.layer_name ?? ""}</span>
  </div>{open && <ul className="data-children" aria-label={`Topics in ${cluster.name}`}>
    {error ? <li className="tree-message"><span className="error">{error}</span> <button className="text-button" onClick={load}>Retry</button></li>
      : !topics ? <li className="tree-message muted" role="status">Loading…</li>
      : !topics.length ? <li className="tree-message muted">No topics.</li>
      : topics.map((t) => <li key={t.id}><button className={`tree-leaf ${activeKey === `kafka:${t.id}` ? "selected" : ""}`} onClick={() => openTab(resourceTab(t))}>
          <ProductIcon kind="topic" size={16} /><span className="tree-name">{t.name}</span>{t.config.phase !== "Ready" && <span className="muted">{String(t.config.phase ?? "")}</span>}</button></li>)}
  </ul>}</li>;
}

function SourceTreeNode({ source, hub, search, activeKey, openTab }: { source: AccessResource; hub: Hub; search: string; activeKey: string; openTab: Props["openTab"] }) {
  const [expanded, setExpanded] = useState(false);
  const open = expanded || !!search;
  const match = (name: string) => name.toLowerCase().includes(search.toLowerCase());
  return <li><div className={`tree-node ${activeKey === `source:${source.name}` ? "selected" : ""}`}>
    <button className="tree-expander" aria-label={`${open ? "Collapse" : "Expand"} data source ${source.name}`} aria-expanded={open} onClick={() => setExpanded(!open)}><IconChevron /></button>
    <button className="tree-label tree-name" onClick={() => { setExpanded(true); openTab(resourceTab(source)); }}><ProductIcon kind={String(source.config.source_kind)} size={16} />{source.name}</button>
    <span className="muted tree-type">{String(source.config.source_kind ?? "")}</span>
  </div>{open && <ul className="data-children">{accessibleSchemas(hub.resources, source.id).filter((r) => match(r.name) || match(source.name)).map((r) => <SchemaTreeNode key={r.id} schema={r} openTab={openTab} selected={activeKey === `schema:${r.id}`} />)}</ul>}</li>;
}

/** APIs by direction; each heading opens that tab of API management. */
function ApisPanel({ hub, openTab, activeKey }: Props) {
  const apis = useCached("catalog:apis", api.apis.list);
  const all = apis.data?.apis ?? [];
  const shown = all.filter((a) => inLayer(hub.layerId, a.config.layer_id ?? a.config.environment_id));
  return <>{(["internal", "external"] as const).map((direction) => <section key={direction}>
    <div className="section-title">
      <button className="section-link" onClick={() => openTab({ kind: "apis", ref: direction, title: "API management" })}>{direction === "internal" ? "Internal" : "External"}</button>
    </div>
    <ul className="list">
      {shown.filter((a) => a.direction === direction).map((a) => <li key={a.id} className={activeKey === `api:${a.id}` ? "selected" : ""}>
        <button className="nav-item" onClick={() => openTab({ kind: "api", ref: a.id, title: a.name })}><IconApi size={16} /><span className="grow">{a.name} <span className="sub">{a.protocol.toUpperCase()}</span></span></button>
      </li>)}
      {apis.status === "ready" && !shown.some((a) => a.direction === direction) && <li className="empty muted">None in this layer</li>}
    </ul>
  </section>)}
    {apis.error && <p className="sidebar-note error">{apis.error}</p>}
    <OtherLayers count={all.length - shown.length} />
  </>;
}

/** Security: the pages carry their own lists and detail; the sidebar only switches between them. */
function SecurityPanel({ openTab, activeKey }: Props) {
  const page = activeKey.split(":")[0];
  const current = page === "service" ? "services" : page;
  return <ul className="list" aria-label="Security pages" style={{ marginTop: 4 }}>
    {SECURITY_PAGES.map((item) => <li key={item.kind} className={current === item.kind ? "selected" : ""}>
      <button className="nav-item" aria-current={current === item.kind ? "page" : undefined} onClick={() => openTab({ kind: item.kind, title: item.title })}>
        <SecurityIcon icon={item.icon} />{item.title}
      </button>
    </li>)}
  </ul>;
}

function ComputePanel({ hub, openTab, activeRef }: Props) {
  const everywhere = hub.usageAllLayers, setEverywhere = hub.setUsageAllLayers;
  const all = hub.me?.pools ?? [];
  const pools = all.filter((p) => everywhere || poolInLayer(p, hub.layerId));
  return (<>
    <div className="segmented sidebar-toggle" role="group" aria-label="Layers shown">
      <button aria-pressed={!everywhere} className={everywhere ? "" : "active"} onClick={() => setEverywhere(false)}>This layer</button>
      <button aria-pressed={everywhere} className={everywhere ? "active" : ""} onClick={() => setEverywhere(true)}>All layers</button>
    </div>
    <ul className="list">
      {pools.map((p) => (
        <li key={p.id} className={activeRef === p.name ? "selected" : ""}>
          <button className="nav-item" onClick={() => openTab({ kind: "pool", ref: p.name, title: p.name })}><span className={`dot ${poolDot(p)}`} /><span className="grow">{p.name} <span className="sub">{p.cpu} vCPU · {p.memory_gb} GB{p.gpu ? ` · ${p.gpu} GPU` : ""}</span></span></button>
          <span className="badge" title={`${p.in_use ?? "—"} of ${p.max_concurrent} slots in use${p.queued ? ` · ${p.queued} waiting` : ""} · ${p.active_runs ?? "—"} jobs · ${p.active_services ?? "—"} service components`}>{p.dedicated ? `${p.in_use ?? "—"} · team` : `${p.in_use ?? "—"}/${p.max_concurrent}${p.queued ? ` +${p.queued}` : ""}`}</span>
        </li>
      ))}
    </ul>
    {!everywhere && <OtherLayers count={all.length - pools.length} />}
  </>);
}

function PackagesPanel({ hub, openTab, activeKey, activeRef }: Props) {
  const items: (TabRef & { label: string; icon: JSX.Element })[] = [
    { kind: "packages", title: "Packages", label: "All packages", icon: <IconPackage size={16} /> },
    { kind: "packages", ref: "python", title: "Python packages", label: "Python", icon: <ProductIcon kind="python" size={16} /> },
    ...(rEnabled(hub.me) ? [{ kind: "packages" as const, ref: "r", title: "R packages", label: "R", icon: <ProductIcon kind="r" size={16} /> }] : []),
    { kind: "package-imports", title: "Package imports", label: "Imports", icon: <IconImport size={16} /> },
  ];
  return <>
    <ul className="list">
      {items.map(({ label, icon, ...t }) => <li key={label} className={activeKey === `${t.kind}:${t.ref ?? ""}` && (activeRef ?? "") === (t.ref ?? "") ? "selected" : ""}>
        <button className="nav-item" onClick={() => openTab(t)}>{icon}{label}</button></li>)}
    </ul>
    <p className="sidebar-note muted">Workspaces and programs install packages only from here. pip, uv, install.packages and renv are already set up to use it.</p>
  </>;
}

function AdminPanel({ openTab, activeKey }: Props) {
  const items: TabRef[] = [
    { kind: "admin-layers", title: "Layers" },
    { kind: "admin-sources", title: "Storage integrations" },
    { kind: "admin-repos", title: "Git repositories" },
    { kind: "admin-runtimes", title: "Runtimes (versions)" },
    { kind: "admin-service-accounts", title: "Service accounts" },
    { kind: "admin-pools", title: "Compute pools" },
    { kind: "admin-workspace-templates", title: "Workspace templates" },
    { kind: "admin-network", title: "Network allowlist" },
    { kind: "admin-query-log", title: "Query log" },
    { kind: "admin-audit", title: "Audit log" },
  ];
  return (
    <ul className="list">
      {items.map((t) => (
        <li key={t.kind} className={activeKey === `${t.kind}:` ? "selected" : ""}>
          <button className="nav-item" onClick={() => openTab(t)}>{t.title}</button>
        </li>
      ))}
    </ul>
  );
}
