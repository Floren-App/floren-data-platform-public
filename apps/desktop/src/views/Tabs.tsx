import { useEffect, useRef, useState } from "react";
import { api, DataLocation, EDITOR_LABEL, inLayer, inLine, isActive, poolDot, poolInLayer, shortSha, openInWindow, OutputContent, OutputFile, Run, RunScope, runtimeLabel, WarehouseOccupant, WarehouseOverview } from "../api";
import { DataObjectView } from "../components/DataBrowser";
import type { Hub } from "../hub";
import { IconRuns, IconCompute, IconOpen, IconPlay, IconStop, IconTrash, IconWorkspaces } from "../components/Icons";
import { PageHeader } from "../components/Page";
import { LoadMoreRuns, NewRunForm, openEditor, RUN_SCOPE_TITLE, runsIn } from "./Sidebar";
import { AdminAppProfiles, AdminNotifications, AdminClassifications, AdminLayers, AdminAudit, AdminNetwork, AdminPools, AdminWorkspaceTemplates } from "./Admin";
import { ProgramRunner } from "./Programs";
import { AdminRepos, WorkloadView, DeployWizard } from "./Deploy";
import { AccessTab } from "./Access";
import { UsersTab } from "./Directory";
import { ProductIcon } from "../components/ProductIcon";
import { DataSourcesView, FolderView, SchemaView, ServiceView, ServicesView, SourceView } from "./Objects";
import type { ObjectSection } from "../components/Permissions";
import { AdminRuntimes, SecretsTab } from "./Identity";
import { CatalogObject, CatalogView } from "./Catalog";
import { AccessRequestsView, ProductsView, ProductView } from "./Products";
import { Home, WorkspacesView } from "./Home";
import { ApisView, ApiView } from "./Apis";
import { ensureGitChoice, GitCredentialsTab, WorkspaceGit } from "./GitCredentials";
import { WorkspaceSections } from "./WorkspaceSections";
import { LogView } from "../components/LogView";
import { PackageImportsView, PackagesView, PackageView } from "./Packages";
import { QueryLogView } from "./QueryLog";
import { rEnabled, type PackageKind } from "../api";

import { KafkaClustersView, KafkaView } from "./Kafka";
import { PrivacyForm, PrivacyForms, PrivacyMemberships, PrivacyPolicies, PrivacyRegister, PrivacyReviews, PrivacyRoles } from "./Privacy";
import { CaseView } from "./Cases";

export type TabKind = "kafka" | "kafka-clusters" | "apis" | "api" | "catalog" | "catalog-object" | "users" | "access" | "home" | "workspaces" | "runs" | "workspace" | "run" | "data" | "source" | "schema" | "data-object" | "pool" | "folder" | "service" | "services" | "deploy" | "workload" | "secrets" | "docs" | "git-credentials" | "packages" | "package" | "package-imports"
  | "products" | "product" | "access-requests" | "app-profiles" | "repos"
  | "privacy-reviews" | "privacy-case" | "case" | "privacy-memberships" | "privacy-register"
  | "privacy-policies" | "privacy-forms" | "privacy-form" | "privacy-roles" | "privacy-classifications"
  | "admin-app-profiles" | "admin-notifications" | "admin-classifications" | "admin-layers" | "admin-sources" | "admin-repos" | "admin-runtimes" | "admin-service-accounts" | "admin-pools" | "admin-workspace-templates" | "admin-network" | "admin-query-log" | "admin-audit";

export interface TabRef {
  kind: TabKind;
  kafkaKind?: "cluster" | "topic";
  ref?: string;
  title: string;
  section?: ObjectSection;
  search?: string;
  intent?: "create-folder" | "create" | "publish" | "request";
  /** Data products: the catalog object to publish (intent "publish"). */
  assetId?: string;
  productSection?: "overview" | "options" | "lineage" | "used-by" | "permissions";
  catalogSection?: "overview" | "lineage" | "governance" | "sla" | "access" | "metrics";
  apiSection?: "operations" | "settings" | "calls" | "permissions";
  /** An endpoint group or operation resource to select on the API page. */
  apiFocus?: string;
  bindingPrincipalId?: string;
  /** For a run: a deployed program's run (Workloads) or a workspace run (Workspaces). */
  runScope?: RunScope;
  grantPrincipalId?: string;
  data?: DataLocation;
  /** A group page's section in the Access area. */
  groupSection?: "members" | "access" | "settings";
}

interface Props {
  tab: TabRef;
  hub: Hub;
  openTab: (t: TabRef) => void;
}

export function TabContent({ tab, hub, openTab }: Props) {
  switch (tab.kind) {
    case "apis": return <ApisView direction={tab.ref === "external" ? "external" : "internal"} hub={hub} openTab={openTab} />;
    case "api": return <ApiView key={tab.ref} tab={tab} hub={hub} openTab={openTab} />;
    case "catalog": return <CatalogView openTab={openTab} initialKind={tab.ref} initialQuery={tab.search} layerId={hub.layerId} />;
    case "products": return <ProductsView key={tab.ref ?? ""} tab={tab} hub={hub} openTab={openTab} />;
    case "product": return <ProductView key={tab.ref} tab={tab} hub={hub} openTab={openTab} />;
    case "access-requests": return <AccessRequestsView key={tab.ref ?? ""} tab={tab} hub={hub} openTab={openTab} />;
    case "catalog-object": return <CatalogObject tab={tab} hub={hub} openTab={openTab} />;
    case "access": return <AccessTab hub={hub} openTab={openTab} groupId={tab.ref} section={tab.groupSection ?? "members"} bindingPrincipalId={tab.bindingPrincipalId}
      onSelect={(id, name, section) => openTab({ kind: "access", ref: id, title: name, bindingPrincipalId: tab.bindingPrincipalId, groupSection: section })} />;
    case "users": return <UsersTab hub={hub} selectedId={tab.ref} openTab={openTab} />;
    case "home": return <Home hub={hub} openTab={openTab} />;
    case "workspaces": return <WorkspacesView hub={hub} openTab={openTab} />;
    case "runs": return tab.ref === "workspace" ? <WorkspaceRuns hub={hub} openTab={openTab} />
      : <FolderView tab={{ kind: "folder", ref: "workloads", title: "Workloads", section: "runs" }} hub={hub} openTab={openTab} />;
    case "workspace": return <WorkspaceTab id={tab.ref!} hub={hub} openTab={openTab} />;
    case "run": return <RunTab id={tab.ref!} hub={hub} openTab={openTab} />;
    case "source": return <SourceView tab={tab} hub={hub} openTab={openTab} />;
    case "kafka": return <KafkaView key={tab.ref} tab={tab} hub={hub} openTab={openTab} />;
    case "kafka-clusters": return <KafkaClustersView hub={hub} openTab={openTab} />;
    case "data": return <DataSourcesView hub={hub} openTab={openTab} />;
    case "schema": return <SchemaView tab={tab} hub={hub} openTab={openTab} />;
    case "data-object": return <DataObjectView tab={tab} hub={hub} openTab={openTab} />;
    case "pool": return <PoolView name={tab.ref} hub={hub} openTab={openTab} />;
    case "folder": return <FolderView tab={tab} hub={hub} openTab={openTab} />;
    case "service": return <ServiceView tab={tab} hub={hub} openTab={openTab} />;
    case "services": case "admin-service-accounts": return <ServicesView hub={hub} openTab={openTab} />;
    case "deploy": return <DeployWizard hub={hub} openTab={openTab} initialFolder={tab.ref} />;
    case "workload": return <WorkloadView tab={tab} id={tab.ref!} hub={hub} openTab={openTab} runTab={runTab} />;
    case "admin-layers": return <AdminLayers hub={hub} />;
    case "app-profiles": case "admin-app-profiles": return <AdminAppProfiles />;
    case "admin-notifications": return <AdminNotifications />;
    case "admin-classifications": case "privacy-classifications": return <AdminClassifications />;
    case "privacy-policies": return <PrivacyPolicies hub={hub} />;
    case "privacy-reviews": return <PrivacyReviews openTab={openTab} />;
    case "privacy-memberships": return <PrivacyMemberships hub={hub} />;
    case "privacy-register": return <PrivacyRegister openTab={openTab} />;
    case "privacy-case": case "case": return <CaseView key={tab.ref} id={tab.ref ?? ""} hub={hub} openTab={(t) => openTab(t.kind === "privacy-case" && tab.kind === "case" ? { ...t, kind: "case" } : t)} />;
    case "privacy-forms": return <PrivacyForms hub={hub} openTab={openTab} />;
    case "privacy-roles": return <PrivacyRoles hub={hub} />;
    case "privacy-form": return <PrivacyForm key={tab.ref} id={tab.ref ?? ""} hub={hub} onRenamed={(title) => openTab({ ...tab, title })}
      onDeleted={() => openTab({ kind: "privacy-forms", title: "Compliance forms" })} />;
    case "admin-sources": return <DataSourcesView hub={hub} openTab={openTab} adding />;
    case "repos": case "admin-repos": return <AdminRepos />;
    case "admin-runtimes": return <AdminRuntimes withR={rEnabled(hub.me)} />;
    case "secrets": return <SecretsTab hub={hub} selectedRef={tab.ref} creating={tab.intent === "create"} openTab={openTab} />;
    case "docs": return <DocsTab runId={tab.ref!} />;
    case "admin-pools": return <AdminPools hub={hub} />;
    case "admin-workspace-templates": return <AdminWorkspaceTemplates hub={hub} />;
    case "admin-network": return <AdminNetwork />;
    case "admin-query-log": return <QueryLogView hub={hub} />;
    case "git-credentials": return <GitCredentialsTab sections={<WorkspaceSections active="git" openTab={openTab} />} />;
    case "packages": return <PackagesView key={tab.ref ?? "all"} hub={hub} openTab={openTab} kind={tab.ref as PackageKind | undefined} />;
    case "package": return <PackageView key={tab.ref} hub={hub} openTab={openTab} reference={tab.ref!} />;
    case "package-imports": return <PackageImportsView />;
    case "admin-audit": return <AdminAudit />;
  }
}

function Usage({ used, max }: { used: number; max: number }) {
  const share = max ? Math.min(1, used / max) : 0;
  return <span className="warehouse-usage"><span className={`meter ${used >= max ? "full" : ""}`} role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={used} aria-label="Slots in use"><span style={{ width: `${share * 100}%` }} /></span>{used} / {max}</span>;
}

const CATEGORY: Record<WarehouseOccupant["category"], string> = { job: "Job", app: "App", airflow: "Airflow", trino: "SQL engine", kafka: "Kafka brokers", workspace: "Workspace" };

function PoolView({ name, hub, openTab }: { name?: string; hub: Hub; openTab: (tab: TabRef) => void }) {
  const [overview, setOverview] = useState<WarehouseOverview[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {  // live while open: occupancy changes as work starts, finishes and leaves the line
    let cancelled = false;
    const load = () => api.warehouses().then((o) => { if (!cancelled) { setOverview(o); setError(""); } })
      .catch((e: Error) => !cancelled && setError(e.message));
    load();
    const t = setInterval(load, 10000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);
  // The layer mode applies unless "All layers" is chosen (shared with the sidebar).
  const all = hub.usageAllLayers, here = (layer?: string | null) => all || inLayer(hub.layerId, layer);
  const pools = (hub.me?.pools ?? []).filter((p) => all || poolInLayer(p, hub.layerId) || p.name === name);
  const pool = pools.find((p) => p.name === name);
  const overviewOf = (n: string) => { const w = overview?.find((o) => o.name === n);
    return w && { ...w, layers: w.layers.filter((l) => here(l.layer_id)), occupants: w.occupants.filter((o) => here(o.layer_id)) }; };
  const view = name ? overviewOf(name) : undefined;
  if (name && !pool) return <p className="muted">Compute pool not available.</p>;
  const runs = hub.runs.filter((r) => (!name || r.pool_name === name) && here(r.layer_id));
  const layerName = hub.layers.find((l) => l.id === hub.layerId)?.name;
  const open = (o: WarehouseOccupant) => {
    if (o.run_id) { const run = hub.runs.find((r) => r.id === o.run_id); openTab(run ? runTab(run) : { kind: "run", ref: o.run_id, title: o.name }); }
    else if (o.workload_id) openTab({ kind: "workload", ref: o.workload_id, title: o.name });
    else if (o.workspace_id) openTab({ kind: "workspace", ref: o.workspace_id, title: o.name });
  };
  return <>
    <PageHeader icon={<IconCompute size={22} />} title={pool?.name ?? "Warehouses"}
      subtitle="Every job, deployed app, Airflow component and Trino worker takes one slot; work beyond the limit waits in line and starts in order. Queries share the Trino workers and workspaces have their own limits. You see details of your own work; the rest is counted."
      actions={<>
        <div className="segmented" role="group" aria-label="Layers shown">
          <button aria-pressed={!all} className={all ? "" : "active"} onClick={() => hub.setUsageAllLayers(false)}>{layerName ?? "This layer"}</button>
          <button aria-pressed={all} className={all ? "active" : ""} onClick={() => hub.setUsageAllLayers(true)}>All layers</button>
        </div>
        {hub.me?.is_admin && <button className="btn secondary" onClick={() => openTab({ kind: "admin-pools", title: "Compute pool settings" })}>Configure pools</button>}</>} />
    {error && <p className="error" role="alert">{error}</p>}
    {pool ? <>
      <section className="card"><dl className="kv"><dt>Size</dt><dd>{pool.cpu} vCPU · {pool.memory_gb} GB{pool.gpu ? ` · ${pool.gpu} GPU` : ""}</dd>
        {pool.dedicated ? <><dt>Dedicated</dt><dd>To {pool.allowed_groups.join(", ")} · no slot limit, never waits in line</dd>
          <dt>In use</dt><dd>{view?.in_use ?? pool.in_use ?? "—"} containers</dd></>
          : <><dt>In use</dt><dd>{view ? <Usage used={view.in_use} max={view.max_concurrent} /> : `${pool.in_use ?? "—"} of ${pool.max_concurrent}`} slots{(view?.queued ?? pool.queued) ? ` · ${view?.queued ?? pool.queued} waiting` : ""}</dd></>}
        {view?.workspaces && <><dt>Workspaces</dt><dd>Editors are sized by this warehouse · {view.workspaces.running} running{view.workspaces.max_running ? ` of ${view.workspaces.max_running}` : ""}{view.workspaces.queued ? ` · ${view.workspaces.queued} waiting` : ""} (separate limit)</dd></>}
        <dt>For</dt><dd>{pool.layer_names?.length ? pool.layer_names.join(", ") : "every layer"} · {pool.workload_kinds?.length ? pool.workload_kinds.join(", ") : "every kind of work"}</dd>
        <dt>Running containers</dt><dd>{pool.active_executions ?? "—"} in the cluster</dd>
        <dt>Auto-suspend</dt><dd>{Math.round(pool.auto_suspend_seconds / 60)} min</dd></dl></section>
      {view && view.layers.length > 0 && <>
        <div className="section-heading"><h2>By layer</h2></div>
        <table aria-label="Use by layer"><thead><tr><th>Layer</th><th>Slots in use</th><th>Waiting</th></tr></thead><tbody>
          {view.layers.map((l) => <tr key={l.layer_id ?? "none"}><td>{l.layer_name ?? "—"}</td><td>{l.in_use}</td><td>{l.queued || "—"}</td></tr>)}
        </tbody></table></>}
      {view && view.kinds?.length > 0 && <>
        <div className="section-heading"><h2>By kind of work</h2></div>
        <table aria-label="Use by kind of work"><thead><tr><th>Kind</th><th>Slots in use</th><th>Waiting</th></tr></thead><tbody>
          {view.kinds.map((k) => <tr key={k.kind}><td>{k.kind === "workers" ? "Trino workers" : k.kind}</td><td>{k.in_use}</td><td>{k.queued || "—"}</td></tr>)}
        </tbody></table></>}
      {view && <>
        <div className="section-heading"><h2>Using this warehouse</h2></div>
        {view.occupants.length ? <div className="table-scroll"><table aria-label="Using this warehouse">
          <thead><tr><th>Name</th><th>Type</th><th>Layer</th><th>Owner</th><th>Status</th><th>Slots</th></tr></thead>
          <tbody>{view.occupants.map((o) => <tr key={`${o.category}:${o.run_id ?? o.workload_id ?? o.workspace_id ?? o.layer_id}:${o.state}`} className={o.category === "trino" ? "" : "clickable"} onClick={() => open(o)}>
            <td>{o.category === "trino" ? o.name : <button className="text-button" onClick={(e) => { e.stopPropagation(); open(o); }}>{o.name}</button>}</td>
            <td>{CATEGORY[o.category]}{o.kind !== o.category && o.category !== "trino" ? ` · ${o.kind}` : ""}</td><td>{o.layer_name ?? "—"}</td><td>{o.owner}</td>
            <td><span className="status"><span className={`dot ${o.state}`} />{o.state === "queued" ? `queued · ${inLine(o.queue_position)}` : "running"}</span></td>
            <td>{o.slots || "—"}</td>
          </tr>)}</tbody></table></div> : <p className="muted">None of your work is using this warehouse.</p>}
        {(view.others_in_use > 0 || view.others_queued > 0) && <p className="muted">Work of others you cannot see: {view.others_in_use} slot{view.others_in_use === 1 ? "" : "s"} in use{view.others_queued ? `, ${view.others_queued} waiting` : ""}.</p>}
      </>}
    </> : <table aria-label="Warehouses"><thead><tr><th>Warehouse</th><th>Size</th><th>Slots in use</th><th>Waiting</th><th>Layers</th></tr></thead><tbody>{pools.map((p) => {
        const w = overviewOf(p.name);
        return <tr key={p.id}>
          <td><span className="status"><span className={`dot ${poolDot(w ?? p)}`} /><button className="text-button object-name" onClick={() => openTab({ kind: "pool", ref: p.name, title: p.name })}>{p.name}</button></span>{w?.workspaces && <span className="badge" title="Editors are sized by this warehouse">workspaces</span>}{p.dedicated && <span className="badge" title={`Dedicated to ${p.allowed_groups.join(", ")}: no slot limit`}>dedicated</span>}</td>
          <td>{p.cpu} vCPU · {p.memory_gb} GB</td>
          <td>{p.dedicated ? `${w?.in_use ?? p.in_use ?? "—"} · no limit` : w ? <Usage used={w.in_use} max={w.max_concurrent} /> : `${p.in_use ?? "—"} / ${p.max_concurrent}`}</td>
          <td>{(w?.queued ?? p.queued) || "—"}</td>
          <td className="muted">{w?.layers.filter((l) => l.in_use || l.queued).map((l) => `${l.layer_name ?? "—"} ${l.in_use}${l.queued ? `+${l.queued}` : ""}`).join(" · ") || "—"}</td>
        </tr>;
      })}</tbody></table>}
    <div className="section-heading"><h2>{pool ? "Your runs on this warehouse" : "Your recent runs"}</h2></div>
    <RunTable runs={runs} openTab={openTab} hub={hub} />
  </>;
}

/** Runs as a table: one shared shape for workspace, pool, and workload pages. */
export function RunTable({ runs, hub, openTab, warehouse = true }: { runs: Run[]; hub: Hub; openTab: (t: TabRef) => void; warehouse?: boolean }) {
  if (!runs.length) return hub.hasMoreRuns ? <p className="muted">None of your recent runs. <LoadMoreRuns hub={hub} /></p> : <p className="muted">No runs yet.</p>;
  return <div className="table-scroll"><table aria-label="Runs">
    <thead><tr><th>Program / file</th><th>Type</th><th>Version</th>{warehouse && <th>Warehouse</th>}<th>Status</th><th>Started</th><th /></tr></thead>
    <tbody>{runs.map((r) => (
      <tr key={r.id} className="clickable" onClick={() => openTab(runTab(r))}>
        <td><button className="text-button mono" onClick={(e) => { e.stopPropagation(); openTab(runTab(r)); }}>{r.program ?? r.entrypoint}</button></td>
        <td>{r.kind}</td><td className="mono" title={r.commit_sha ?? "Workspace files (no commit)"}>{r.commit_sha ? shortSha(r.commit_sha) : <span className="muted">workspace</span>}</td>{warehouse && <td>{r.pool_name}</td>}
        <td><span className="status" title={r.waiting_reason ?? undefined}><span className={`dot ${r.status}`} />{r.status === "queued" ? `queued · ${inLine(r.queue_position)}` : r.status}</span>
          {r.waiting_reason && <div className="muted small">{r.waiting_reason}</div>}</td>
        <td className="muted">{new Date(r.created_at).toLocaleString()}</td>
        <td className="table-actions" onClick={(e) => e.stopPropagation()}>
          {isActive(r) && <button className="btn sm secondary" onClick={() => hub.act(() => api.stopRun(r.id))}><IconStop size={14} />Stop</button>}
        </td>
      </tr>
    ))}</tbody>
  </table>{hub.hasMoreRuns && <div className="row pagination"><span className="muted">Showing your {hub.runs.length} most recent runs</span><LoadMoreRuns hub={hub} className="btn sm secondary" /></div>}</div>;
}

/** Runs with search and a status filter: the Runs tab of the Workloads folders and of Workspaces. */
export function RunList({ runs, hub, openTab }: { runs: Run[]; hub: Hub; openTab: (tab: TabRef) => void }) {
  const [query, setQuery] = useState(""), [status, setStatus] = useState("");
  const shown = runs.filter((r) => (!status || (status === "active" ? isActive(r) : r.status === status)) &&
    `${r.program ?? ""} ${r.entrypoint} ${r.pool_name} ${hub.workloads.find((p) => p.id === r.workload_id)?.title ?? ""}`.toLowerCase().includes(query.toLowerCase()));
  return <>
    <div className="object-toolbar"><input type="search" aria-label="Search recent runs" placeholder="Search programs, files, or pools…" value={query} onChange={(e) => setQuery(e.target.value)} />
      <select aria-label="Run status" value={status} onChange={(e) => setStatus(e.target.value)}><option value="">All statuses</option><option value="active">Active</option>{["succeeded", "failed", "stopped"].map((value) => <option key={value}>{value}</option>)}</select></div>
    {!shown.length && runs.length ? <p className="muted">No loaded runs match these filters.{hub.hasMoreRuns && <> <LoadMoreRuns hub={hub} /></>}</p> : <RunTable runs={shown} hub={hub} openTab={openTab} />}
  </>;
}

/** Runs started in workspaces; runs of deployed programs are the Runs tab of the Workloads folders. */
function WorkspaceRuns({ hub, openTab }: { hub: Hub; openTab: (tab: TabRef) => void }) {
  const [adding, setAdding] = useState(false);
  const runs = runsIn(hub.runs, "workspace").filter((r) => inLayer(hub.layerId, r.layer_id));
  return <>
    <PageHeader icon={<IconRuns size={22} />} title={RUN_SCOPE_TITLE.workspace}
      subtitle="Runs started in your workspaces, in this layer. Only the workspace's owner and administrators see them. Open a run for its console and outputs."
      actions={!!hub.workspaces.length && <button className="btn" onClick={() => setAdding(!adding)} aria-expanded={adding}><IconPlay size={14} />New run</button>} />
    <WorkspaceSections active="runs" openTab={openTab} />
    {adding && <div className="card"><NewRunForm hub={hub} onDone={() => setAdding(false)} /></div>}
    <RunList runs={runs} hub={hub} openTab={openTab} />
  </>;
}

function WorkspaceTab({ id, hub, openTab }: { id: string; hub: Hub; openTab: (t: TabRef) => void }) {
  const [requestingStart,setRequestingStart] = useState(false);
  const ws = hub.workspaces.find((w) => w.id === id);
  if (!ws) return <p className="muted">Workspace not found (it may have been deleted).</p>;
  const runs = hub.runs.filter((r) => r.workspace_id === id);
  const starting = ws.status === "starting" || requestingStart;
  const start = async () => {
    setRequestingStart(true);
    try { if (await ensureGitChoice(ws)) await hub.act(() => api.startWorkspace(ws.id)); }
    finally {setRequestingStart(false);}
  };
  return (
    <>
      <PageHeader icon={<IconWorkspaces size={22} />} title={ws.name}
        subtitle={<><span className={`dot ${ws.status}`} />{ws.status === "queued" ? `queued · ${inLine(ws.queue_position)}` : ws.status} · {EDITOR_LABEL[ws.editor] ?? ws.editor}{ws.layer_name && <> · Layer: <strong>{ws.layer_name}</strong></>}</>}
        actions={<>
          <button className="btn ghost danger" title="Files in your home volume are kept"
                  onClick={() => confirm(`Delete workspace ${ws.name}? Files in your home volume are kept.`) && hub.act(() => api.deleteWorkspace(ws.id))}>
            <IconTrash size={14} />Delete
          </button>
          <span className="divider" />
          {ws.status === "running" || ws.status === "starting" || ws.status === "queued" ? (
            <>
              <button className="btn secondary" onClick={() => hub.act(() => api.stopWorkspace(ws.id))}><IconStop size={14} />{ws.status === "queued" ? "Leave the line" : "Stop"}</button>
              <button className="btn" disabled={starting || ws.status === "queued"} onClick={() => openEditor(hub, ws.id, ws.name)}><IconOpen size={14} />{starting ? "Starting…" : "Open editor"}</button>
            </>
          ) : (
            <button className="btn" disabled={requestingStart} onClick={() => void start()}><IconPlay size={14} />{requestingStart ? "Starting…" : ws.status === "failed" ? "Retry start" : "Start"}</button>
          )}
        </>} />
      {ws.status === "queued" && <section className="card workspace-starting" role="status" aria-live="polite">
        <h2>Waiting for a free workspace slot · {inLine(ws.queue_position)}</h2>
        <p>Your administrator limits how many workspaces run at once. This one starts by itself when a slot frees up; this page updates automatically.</p>
      </section>}
      {starting && <section className="card workspace-starting" role="status" aria-live="polite">
        <h2>Starting {EDITOR_LABEL[ws.editor] ?? "workspace"}…</h2>
        <p>{ws.waiting_reason ? <>Not started yet: <strong>{ws.waiting_reason}</strong>. It starts by itself when there is room; stop other workspaces or jobs to free some.</> : "The editor is preparing. Open editor becomes available when it is ready; this page updates automatically."}</p>
        {ws.started_at && <p className="muted">Started {new Date(ws.started_at).toLocaleTimeString()}. You can stop the workspace while it starts.</p>}
      </section>}
      {ws.status === "failed" && <section className="card" role="alert"><h2>Workspace could not start</h2><p>{ws.startup_error || "The editor stopped unexpectedly. Try starting it again."}</p><p className="muted">Your saved files are kept.</p></section>}
      <div className="card">
        <dl className="kv">
          {ws.owner_group_name && <><dt>Owner group</dt><dd>{ws.owner_group_name}</dd></>}
          <dt>Created by</dt><dd>{ws.owner}</dd>
          <dt>Size</dt><dd>Editor tier (0.5 vCPU · 1 GB). Suspends after 30 min idle.</dd>
          <dt>Last activity</dt><dd>{new Date(ws.last_activity_at).toLocaleString()}</dd>
          <dt>Files</dt><dd><code>/workspace</code> — shared with every run you start from here.</dd>
        </dl>
        <WorkspaceGit ws={ws} onChanged={hub.refresh} />
      </div>
      {ws.editor === "sql" ? <section className="card"><h2>SQL</h2>
        <p className="muted">Browse every catalog and schema you have a data role on, write queries with completion, and keep them as
          <code>.sql</code> scripts. Queries run on the shared engine as you, with the same governance and audit as programs.
          Results stay on the server: export and file upload are disabled.</p>
        <p className="muted">Scripts are saved on this workspace's private volume and survive stops and restarts.</p>
      </section> : <>
        <ProgramRunner workspaceId={ws.id} running={ws.status === "running"} hub={hub} openTab={openTab}
                       onStarted={(r) => openTab(runTab(r))} />
        <details className="card">
          <summary>Run a file without fdp.json</summary>
          <NewRunForm hub={hub} workspaceId={ws.id} />
        </details>
      </>}
      {runs.length > 0 && <>
        <div className="section-heading"><h2>Runs from this workspace</h2></div>
        <RunTable runs={runs} hub={hub} openTab={openTab} />
      </>}
    </>
  );
}

export const runTab = (r: Run): TabRef => ({ kind: "run", ref: r.id, title: r.program ?? r.entrypoint.split("/").pop() ?? r.id,
  runScope: r.workload_id ? "deployed" : "workspace" });

function RunTab({ id, hub, openTab }: { id: string; hub: Hub; openTab: (t: TabRef) => void }) {
  const run = hub.runs.find((r) => r.id === id);
  const [logs, setLogs] = useState("");
  const [follow, setFollow] = useState(true);
  const [stopping, setStopping] = useState(false);
  const consoleRef = useRef<HTMLPreElement>(null);
  const live = run ? isActive(run) : false;

  // Live console: poll quickly while running, once more when it finishes.
  useEffect(() => {
    let cancelled = false;
    const load = () => api.runLogs(id).then((l) => !cancelled && setLogs(l)).catch(() => undefined);
    load();
    const t = live ? setInterval(load, 1500) : undefined;
    return () => { cancelled = true; if (t) clearInterval(t); };
  }, [id, live]);

  useEffect(() => {
    if (follow && consoleRef.current) consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
  }, [logs, follow]);

  if (!run) return <p className="muted">Run not found.</p>;
  const ws = hub.workspaces.find((w) => w.id === run.workspace_id);
  const pub = hub.workloads.find((p) => p.id === run.workload_id);
  const source = pub ? `deployed ${pub.title} @ ${run.commit_sha?.slice(0, 7)}` : run.workload_id ? "deployed program" : ws?.name ?? "workspace";
  const argList = Object.entries(run.args ?? {});

  const stop = async () => {
    setStopping(true);
    await hub.act(() => api.stopRun(run.id));
    setStopping(false);
  };

  const title = run.program ?? run.entrypoint;
  return (
    <>
      <PageHeader icon={<ProductIcon kind={run.kind} runtime={run.runtime ?? undefined} size={22} />} title={<span className="mono">{title}</span>}
        subtitle={<>
          <span className={`dot ${run.status}`} />
          {run.status === "queued" ? `queued · ${inLine(run.queue_position)}` : run.status}{run.waiting_reason ? ` (${run.waiting_reason})` : ""} · {run.kind} on warehouse <b>{run.pool_name}</b>{run.runtime && ` · ${runtimeLabel(run.runtime, run.runtime_version)}`} · {source}
          {run.owner.startsWith("sa:") && ` · as ${run.owner}`} · started {new Date(run.created_at).toLocaleString()}
          {run.finished_at && ` · ended ${new Date(run.finished_at).toLocaleTimeString()}`}
        </>}
        actions={<>
          {live && (
            <button className="btn danger" onClick={stop} disabled={stopping}>
              <IconStop size={14} />{stopping ? "Stopping…" : "Stop"}
            </button>
          )}
          {!live && run.program && (
            <button className="btn secondary" onClick={() => hub.act(async () => {
              const again = run.workload_id
                ? await api.runDeployed(run.workload_id, run.args, run.pool_name)
                : await api.runProgram(run.workspace_id!, run.program!, run.args, run.pool_name);
              await hub.refresh();
              openTab(runTab(again));
            })}><IconPlay size={14} />Run again</button>
          )}
          {run.kind === "dbt" && !live && (
            <button className="btn" onClick={() => openTab({ kind: "docs", ref: run.id, title: `docs · ${run.program ?? "dbt"}` })}>
              <IconOpen size={14} />Open dbt docs</button>
          )}
          {(run.kind === "app" || run.kind === "airflow") && run.status === "running" && (
            <button className="btn" onClick={() => hub.act(() =>
              openInWindow(api.openApp(run.id).then((r) => r.url), `${title} — Floren Data Platform`, `app-${run.id}`))}><IconOpen size={14} />{run.kind === "airflow" ? "Open Airflow" : "Open app"}</button>
          )}
        </>}>
        {argList.length > 0 && (
          <div className="page-meta">
            {argList.map(([k, v]) => <span key={k} className="badge mono">{k}={String(v)}</span>)}
          </div>
        )}
      </PageHeader>

      <div className="section-heading">
        <h2>Console</h2>
        <label className="row muted" style={{ fontSize: 12 }}>
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> Follow output
        </label>
      </div>
      <pre ref={consoleRef} className="terminal">{logs || (live ? "Waiting for output…" : "No output.")}</pre>

      <div className="section-heading"><h2>Log</h2><span className="muted" style={{ fontSize: 12 }}>Platform events and <code>fdp.log</code> lines of this run</span></div>
      <LogView scope={{ run_id: run.id }} openTab={openTab} showWorkload={false} label="Log of this run" />

      {/* Deployed runs keep outputs on the server's data volume, not in a workspace. */}
      {!live && <Outputs run={run} workspaceRunning={!!run.workload_id || ws?.status === "running"} />}
    </>
  );
}

function Outputs({ run, workspaceRunning }: { run: Run; workspaceRunning: boolean }) {
  const [files, setFiles] = useState<OutputFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<OutputContent | null>(null);

  useEffect(() => {
    setPreview(null);
    if (!workspaceRunning) return;
    api.runOutputs(run.id).then(setFiles).catch((e: Error) => setError(e.message));
  }, [run.id, run.status, workspaceRunning]);

  const show = (path: string) => api.runOutput(run.id, path).then(setPreview).catch((e: Error) => setError(e.message));

  return (
    <>
      <div className="section-heading"><h2>Outputs</h2>
        {!run.workload_id && <span className="muted" style={{ fontSize: 12 }}>In the editor: <code>.fdp/runs/{run.id}/outputs</code></span>}</div>
      {!workspaceRunning ? <p className="muted">Start the workspace to browse this run's outputs (they live on its volume).</p>
        : error ? <p className="error" role="alert">{error}</p>
        : files === null ? <p className="muted">Loading…</p>
        : files.length === 0 ? <p className="muted">No output files. Programs write them to <code>$FDP_OUTPUT_DIR</code> (<code>fdp.output_dir()</code>).</p>
        : (
          <div className="outputs">
            <ul className="list" aria-label="Output files">
              {files.map((f) => (
                <li key={f.path} className={preview?.path === f.path ? "selected" : ""}>
                  <button className="nav-item" onClick={() => show(f.path)}><span className="grow mono">{f.path}</span><span className="sub">{formatSize(f.size)}</span></button>
                </li>
              ))}
            </ul>
            <div className="preview">{preview ? <Preview file={preview} /> : <p className="muted">Select a file to preview it.</p>}</div>
          </div>
        )}
    </>
  );
}

function Preview({ file }: { file: OutputContent }) {
  const ext = file.path.split(".").pop()?.toLowerCase() ?? "";
  const images: Record<string, string> = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", svg: "image/svg+xml", webp: "image/webp" };
  if (images[ext]) return <img alt={file.path} src={`data:${images[ext]};base64,${file.content_base64}`} style={{ maxWidth: "100%" }} />;
  const text = new TextDecoder().decode(Uint8Array.from(atob(file.content_base64), (c) => c.charCodeAt(0)));
  const note = file.truncated && <p className="muted">Showing the first {formatSize(text.length)} of {formatSize(file.size)}.</p>;
  if (ext === "csv" || ext === "tsv") {
    const rows = text.split(/\r?\n/).filter(Boolean).slice(0, 200).map((l) => l.split(ext === "csv" ? "," : "\t"));
    return (
      <>
        {note}
        <table><thead><tr>{rows[0]?.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
          <tbody>{rows.slice(1).map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody></table>
      </>
    );
  }
  if (ext === "ipynb") return <p className="muted">Executed notebook: open it in the editor to see all cell outputs.</p>;
  return <>{note}<pre className="code">{text}</pre></>;
}

function formatSize(n: number): string {
  return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
}

/** dbt docs (single static page with lineage) from a run's outputs, rendered in a sandboxed frame. */
function DocsTab({ runId }: { runId: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.runOutput(runId, "docs/index.html")
      .then((f) => setHtml(new TextDecoder().decode(Uint8Array.from(atob(f.content_base64), (c) => c.charCodeAt(0)))))
      .catch((e: Error) => setError(e.message.includes("not found") ? "This run produced no docs (did dbt fail before docs generate?)." : e.message));
  }, [runId]);
  if (error) return <p className="error" role="alert">{error}</p>;
  if (html === null) return <p className="muted">Loading docs…</p>;
  return <iframe title="dbt docs" srcDoc={html} sandbox="allow-scripts allow-popups" style={{ width: "100%", height: "calc(100vh - 140px)", border: 0, background: "#fff" }} />;
}
