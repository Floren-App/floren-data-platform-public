import { ObjectLineage } from "../components/ObjectLineage";
import { useEffect, useState } from "react";
import { accessibleSchemas, accessibleSources, ago, api, AccessResource, inLayer, ServiceAccount, shortSha } from "../api";
import type { Hub } from "../hub";
import { ObjectBreadcrumbs, ObjectTabs, OpenTab, PermissionsPanel, resourceChain, resourcePath, resourceTab } from "../components/Permissions";
import { RunList, type TabRef } from "./Tabs";
import { TokensCard } from "./Identity";
import { DataListing } from "../components/DataBrowser";
import { DataPoliciesPanel } from "../components/DataPolicies";
import { MetadataSql } from "../components/MetadataSql";
import { ProductIcon } from "../components/ProductIcon";
import { IconBranch, IconPlus, IconDeployed } from "../components/Icons";
import { PageHeader } from "../components/Page";

interface ObjectProps { tab: TabRef; hub: Hub; openTab: OpenTab }

export function DataSourcesView({ hub, openTab }: { hub: Hub; openTab: OpenTab }) {
  const all = accessibleSources(hub.resources);
  const sources = all.filter((s) => inLayer(hub.layerId, s.layer_id));
  return <><PageHeader icon={<ProductIcon kind="data" size={22} />} title="Storage" subtitle="Storage connections contain schemas or storage namespaces. Expand them to browse tables, views, files and columns."
      actions={hub.me?.is_admin && <button className="btn secondary" onClick={() => openTab({ kind: "admin-sources", title: "Storage integrations" })}>Storage integrations</button>} />
    <table><thead><tr><th>Storage connection</th><th>Type</th><th>Layer</th><th>Schemas you can access</th></tr></thead><tbody>{sources.map((s) => <tr key={s.id}>
      <td><button className="text-button object-name" onClick={() => openTab(resourceTab(s))}><ProductIcon kind={String(s.config.source_kind)} size={16} />{s.name}</button></td>
      <td>{String(s.config.source_kind ?? "")}</td><td>{s.layer_name || "—"}</td><td>{accessibleSchemas(hub.resources, s.id).length}</td>
    </tr>)}{!sources.length && <tr><td colSpan={4} className="muted">{all.length ? "No storage connections in this layer." : "No data sources you can access. Ask a data source owner for access."}</td></tr>}</tbody></table>
    {all.length > sources.length && <p className="muted">{all.length - sources.length} more in other layers. Switch the layer in the top bar to see them.</p>}
  </>;
}

function ObjectAccess({ resource, hub, openTab, initialServiceId }: { resource: AccessResource; hub: Hub; openTab: OpenTab; initialServiceId?: string }) {
  return <PermissionsPanel resource={resource} resources={hub.resources} openTab={openTab} onChanged={hub.refresh} initialServiceId={initialServiceId} />;
}

export function SourceView({ tab, hub, openTab }: ObjectProps) {
  const source = hub.resources.find((r) => r.kind === "source" && r.name === tab.ref);
  const ds = hub.me?.data_sources.find((d) => d.name === tab.ref);
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [prefix, setPrefix] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!source) return <p className="muted">Data source not available.</p>;
  const section = tab.section ?? "overview";
  const sourceKind = String(source.config.source_kind ?? ds?.kind ?? "");
  const schemas = accessibleSchemas(hub.resources, source.id);
  const visible = schemas.filter((r) => r.name.toLowerCase().includes(search.toLowerCase()));

  const register = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      const schema = await api.access.schema(source.name, name.trim(), sourceKind === "s3" ? prefix : undefined);
      await hub.refresh(); setAdding(false); setName(""); setPrefix("");
      openTab(resourceTab(schema));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <>
    <ObjectBreadcrumbs resource={source} resources={hub.resources} openTab={openTab} />
    <PageHeader icon={<ProductIcon kind={sourceKind} size={22} />} title={source.name} subtitle={<>Storage connection · {sourceKind}{ds?.description && ` · ${ds.description}`}</>}
      actions={hub.me?.is_admin && <button className="btn secondary" onClick={() => openTab({ kind: "admin-sources", title: "Storage integrations" })}>Integration settings</button>} />
    {source.layer_name && <p className="hint">Layer · <strong>{source.layer_name}</strong> · Schemas and data objects inherit this layer.</p>}
    {source.owner_group_name && <p className="hint">Owner group · <strong>{source.owner_group_name}</strong></p>}
    <ObjectTabs active={section} lineage={source.actions.includes("read")} overview="Schemas & usage" onChange={(s) => openTab({ ...tab, section: s })} />
    {section === "permissions" ? <ObjectAccess resource={source} hub={hub} openTab={openTab} initialServiceId={tab.grantPrincipalId} /> : section === "lineage" ? <ObjectLineage resourceId={source.id} openTab={openTab} /> : <>
      <div className="section-heading" style={{ marginTop: 0 }}><div><h2>{sourceKind === "s3" ? "Storage namespaces" : "Schemas"} <span className="badge">{schemas.length}</span></h2>
        <p className="muted">Open a schema to manage its permissions and access setup.</p></div>
      </div>
      {adding && <form className="card" onSubmit={register}>
        <h3>{sourceKind === "s3" ? "Register a storage namespace" : "Register a schema"}</h3>
        <p className="muted">New scopes inherit this data source's roles. You can add grants on the schema after registration.</p>
        <div className="arg-grid"><div className="field"><label htmlFor="schema-name">{sourceKind === "s3" ? "Namespace name" : "Schema name"}</label>
          <input id="schema-name" autoFocus required value={name} onChange={(e) => setName(e.target.value)} placeholder="analytics" /></div>
          {sourceKind === "s3" && <div className="field"><label htmlFor="schema-prefix">Relative storage prefix</label>
            <input id="schema-prefix" value={prefix} onChange={(e) => setPrefix(e.target.value)} placeholder="Empty uses the source prefix" /></div>}</div>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="form-actions">
          <button className="btn" disabled={busy || !name.trim()}>{busy ? "Registering…" : "Register"}</button>
          <button type="button" className="btn secondary" onClick={() => { setAdding(false); setError(""); }}>Cancel</button>
        </div>
      </form>}
      <div className="object-toolbar"><input type="search" aria-label="Search schemas" placeholder="Search schemas…" value={search} onChange={(e) => setSearch(e.target.value)} />
        {source.actions.includes("assign") && <button className="btn secondary" aria-expanded={adding} onClick={() => setAdding(true)}><IconPlus size={14} />{sourceKind === "s3" ? "Register namespace" : "Register schema"}</button>}</div>
      <div className="table-scroll"><table><thead><tr><th>Schema</th><th>Access setup</th><th /></tr></thead><tbody>
        {visible.map((r) => <tr key={r.id}>
          <td><button className="text-button object-name" onClick={() => openTab(resourceTab(r))}>{r.name}</button>
            {typeof r.config.prefix === "string" && <div className="muted mono">{r.config.prefix || "Source prefix"}</div>}</td>
          <td><SetupState resource={r} /></td>
          <td className="table-actions"><button className="btn sm secondary" onClick={() => openTab(resourceTab(r, "permissions"))}>Permissions</button></td>
        </tr>)}
        {!visible.length && <tr><td colSpan={3} className="muted">{schemas.length ? "No schemas match your search." : "No schemas you can access are registered here."}</td></tr>}
      </tbody></table></div>
      {source.config.system_metadata ? <section className="card"><h2>Platform metadata</h2>
        <p>Open the public schema and its SQL tab to query metadata or manage permissions. Access requires Global Admin as your active group.</p>
        <p className="muted">The database connection is deployment managed through FDP_DATABASE_URL. The local default uses Floren Data Platform’s PostgreSQL instance.</p>
      </section> : <details className="card"><summary>Connect from a workspace</summary>
        <p className="muted">Each request uses the current execution identity and its schema roles.</p>
        <h3 style={{ marginTop: 12 }}>Python</h3><pre className="code">{`import fdp\n\nengine = fdp.connect(${JSON.stringify(source.name)})`}</pre>
        <h3>R</h3><pre className="code">{`con <- fdp::connect(${JSON.stringify(source.name)})`}</pre>
      </details>}
    </>}
  </>;
}

function SetupState({ resource }: { resource: AccessResource }) {
  const state = String(resource.config.state ?? "not provisioned");
  return <span className="status"><span className={`dot ${state === "ready" ? "running" : state === "failed" ? "failed" : "pending"}`} />{state}</span>;
}

export function SchemaView({ tab, hub, openTab }: ObjectProps) {
  const resource = hub.resources.find((r) => r.id === tab.ref && r.kind === "schema");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  if (!resource) return <p className="muted">Schema not available.</p>;
  const source = hub.resources.find((r) => r.id === resource.parent_id);
  const sourceKind = source?.config.source_kind;
  const section = tab.section ?? "overview";
  const provision = async () => {
    if (sourceKind === "postgres" && !confirm("Provision database permissions? This transfers schema/table ownership to managed roles and replaces PUBLIC access within this schema.")) return;
    setBusy(true); setError(""); setMessage("");
    try { await api.access.provision(resource.id); await hub.refresh(); setMessage("Access setup completed."); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <>
    <ObjectBreadcrumbs resource={resource} resources={hub.resources} openTab={openTab} />
    <PageHeader icon={<ProductIcon kind={sourceKind === "s3" ? "bucket" : "data"} size={22} />} title={resource.name}
      subtitle={<>{sourceKind === "s3" ? "Storage namespace" : "Schema"} in {source?.name ?? "data source"} · <SetupState resource={resource} /></>} />
    {resource.layer_name && <p className="hint">Layer · <strong>{resource.layer_name}</strong></p>}
    {resource.owner_group_name && <p className="hint">Owner group · <strong>{resource.owner_group_name}</strong></p>}
    <ObjectTabs active={section} lineage sql={!!resource.config.system_metadata} onChange={(s) => openTab({ ...tab, section: s })}
      policies={(sourceKind === "postgres" || sourceKind === "iceberg") && !resource.config.system_metadata} />
    {section === "policies" ? <DataPoliciesPanel schema={resource} />
      : section === "permissions" ? <ObjectAccess resource={resource} hub={hub} openTab={openTab} initialServiceId={tab.grantPrincipalId} />
      : section === "lineage" ? <ObjectLineage resourceId={resource.id} openTab={openTab} />
      : section === "sql" && resource.config.system_metadata ? <MetadataSql schema={resource} hub={hub} /> : <>
      <DataListing schema={resource} storage={sourceKind === "s3"} openTab={openTab} />
      <div className="arg-grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16, margin: 0 }}>
      <section className="card" style={{ margin: 0 }}><h2>Schema scope</h2>
        <p>Permissions on <strong>{resource.name}</strong> cover all current and future objects in this schema.</p>
        <p className="muted">Roles inherited from {source?.name ?? "the data source"} always apply. Owners can add roles for this schema.</p>
        <div className="form-actions"><button className="btn secondary" onClick={() => openTab({ ...tab, section: "permissions" })}>View permissions</button></div>
      </section>
      <section className="card" style={{ margin: 0 }}><h2>Access setup</h2>
        <dl className="kv"><dt>Status</dt><dd><SetupState resource={resource} /></dd>
          {resource.config.executor === "trino" && <><dt>Query engine</dt><dd>Trino</dd><dt>Catalog</dt><dd className="mono">{String(resource.config.catalog ?? "—")}</dd></>}
          {typeof resource.config.prefix === "string" && <><dt>Relative prefix</dt><dd className="mono">{resource.config.prefix || "Source prefix"}</dd></>}
        </dl>
        {sourceKind === "postgres" && <p className="muted">Setup manages schema and table ownership. Native Admin privileges can include native grants where the database couples them to DDL.</p>}
        {error && <p className="error" role="alert">{error}</p>}
        {message && <p className="success-message" role="status">{message}</p>}
        {resource.actions.includes("assign") && <div className="form-actions"><button className={`btn ${resource.config.state === "ready" ? "secondary" : ""}`} disabled={busy} onClick={provision}>{busy ? "Setting up…" : resource.config.state === "ready" ? "Reapply access setup" : "Set up access"}</button></div>}
      </section>
      </div>
    </>}
  </>;
}

export function FolderView({ tab, hub, openTab }: ObjectProps) {
  const resource = hub.resources.find((r) => r.id === tab.ref && (r.kind === "root" || r.kind === "folder"));
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [layerId, setLayerId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (tab.intent === "create-folder" && resource) {
      setAdding(resource.actions.includes("create"));
      openTab({ ...tab, section: "overview", intent: undefined });
    }
  }, [tab.intent, resource, openTab, tab]);
  if (!resource) return <p className="muted">Folder not available.</p>;
  // The layer mode applies here as in the sidebar: folders and workloads of other layers are hidden.
  const children = hub.resources.filter((r) => r.parent_id === resource.id && (r.kind === "folder" ? inLayer(hub.layerId, r.layer_id)
      : hub.workloads.some((p) => p.resource_id === r.id && p.status !== "stopped" && inLayer(hub.layerId, p.layer_id))))
    .sort((a, b) => Number(b.kind === "folder") - Number(a.kind === "folder") || a.name.localeCompare(b.name));
  const section = tab.section ?? "overview";
  // Runs of the workloads in this folder and its subfolders, in the layer mode.
  const inside = new Set(hub.resources.filter((r) => resourceChain(r, hub.resources).some((a) => a.id === resource.id)).map((r) => r.id));
  const runs = hub.runs.filter((r) => { const w = hub.workloads.find((p) => p.id === r.workload_id);
    return !!w && (resource.kind === "root" || inside.has(w.resource_id ?? "")) && inLayer(hub.layerId, r.layer_id ?? w.layer_id); });
  const create = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      const folder = await api.access.folder(name.trim(), resource.id, layerId || undefined);
      await hub.refresh(); setName(""); setAdding(false);
      openTab(resourceTab(folder, folder.actions.includes("assign") ? "permissions" : "overview"));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <>
    <ObjectBreadcrumbs resource={resource} resources={hub.resources} openTab={openTab} />
    <PageHeader icon={resource.kind === "root" ? <IconDeployed size={22} /> : <ProductIcon kind="folder" size={22} />} title={resource.name}
      subtitle={resource.kind === "root" ? "Organize deployed apps and programs into folders, with permissions on each folder." : "Apps, programs, and nested folders inherit this folder's permissions."}
      actions={resource.actions.includes("create") && <>
        <button className="btn secondary" aria-expanded={adding} onClick={() => { setAdding(true); if (section !== "overview") openTab({ ...tab, section: "overview" }); }}><IconPlus size={14} />New folder</button>
        <button className="btn" onClick={() => openTab({ kind: "deploy", ref: resource.id, title: "Deploy" })}><IconBranch size={14} />Deploy here</button>
      </>} />
    {resource.layer_name && <p className="hint">Layer · <strong>{resource.layer_name}</strong></p>}
    {resource.owner_group_name && <p className="hint">Owner group · <strong>{resource.owner_group_name}</strong></p>}
    {!!resource.config.environment && <p className="hint">Branch policy · Deploy from <code>{String(resource.config.branch)}</code>. Promotion creates a separate instance in its target layer.</p>}
    <ObjectTabs active={section} overview="Contents" runs settings={resource.kind === "folder" && resource.actions.includes("delete")}
      onChange={(s) => openTab({ ...tab, section: s })} />
    {section === "runs" ? <RunList runs={runs} hub={hub} openTab={openTab} />
      : section === "permissions" ? <ObjectAccess resource={resource} hub={hub} openTab={openTab} />
      : section === "settings" ? <OrganizationSettings resource={resource} hub={hub} openTab={openTab} /> : <>
        {adding && <form className="card" onSubmit={create}><h3>New folder in {resource.name}</h3>
          <div className="field"><label htmlFor="folder-name">Folder name</label><input id="folder-name" autoFocus required maxLength={256} value={name} onChange={(e) => setName(e.target.value)} placeholder={resource.kind === "root" ? "Finance" : "tst, acp, prd, or a team name"} /></div>
          {!resource.layer_id && <div className="field"><label htmlFor="folder-layer">Layer</label><select id="folder-layer" value={layerId} onChange={(e) => setLayerId(e.target.value)}><option value="">Organization folder · contains multiple layers</option>{hub.layers.map((layer) => <option key={layer.id} value={layer.id}>{layer.name}</option>)}</select></div>}
          <p className="muted">The new folder inherits parent permissions. Assign additional app groups on its Permissions tab after creation.</p>
          {error && <p className="error" role="alert">{error}</p>}
          <div className="form-actions">
            <button className="btn" disabled={busy || !name.trim()}>{busy ? "Creating…" : "Create folder"}</button>
            <button type="button" className="btn secondary" onClick={() => { setAdding(false); setName(""); setError(""); }}>Cancel</button>
          </div></form>}
        <div className="object-toolbar"><input type="search" aria-label="Search folder contents" placeholder="Search this folder…" value={search} onChange={(e) => setSearch(e.target.value)} /></div>
        <div className="table-scroll"><table><thead><tr><th>Name</th><th>Type</th><th>Layer</th><th>Git</th><th>Updated</th><th /></tr></thead><tbody>
          {children.filter((r) => r.name.toLowerCase().includes(search.toLowerCase())).map((r) => {
            const workload = hub.workloads.find((p) => p.resource_id === r.id);
            return <tr key={r.id}>
            <td><button className="text-button object-name" onClick={() => openTab(resourceTab(r))}><ProductIcon kind={r.kind === "folder" ? "folder" : workload?.kind} runtime={workload?.runtime} size={16} />{r.name}</button></td>
            <td>{r.kind === "folder" ? "Folder" : workload?.kind ?? "Service"}</td>
            <td>{r.layer_name || workload?.layer_name || "—"}</td><td>{workload ? <><span className="mono">{workload.branch} · <span title={workload.deployed_sha ?? undefined}>{shortSha(workload.deployed_sha)}</span></span><div className="muted">{workload.repo} · {workload.program}</div></> : <span className="muted">—</span>}</td>
            <td title={workload?.deployed_at ? new Date(workload.deployed_at).toLocaleString() : undefined}>{workload ? ago(workload.deployed_at) : <span className="muted">—</span>}</td>
            <td className="table-actions"><button className="btn sm secondary" onClick={() => openTab(resourceTab(r, "permissions"))}>Permissions</button></td>
          </tr>; })}
          {!children.length && <tr><td colSpan={6} className="muted">This folder is empty. Use New folder to group teams or environments, or Deploy here to add a program from a Git branch.</td></tr>}
          {!!children.length && !children.some((r) => r.name.toLowerCase().includes(search.toLowerCase())) && <tr><td colSpan={6} className="muted">No items match your search.</td></tr>}
        </tbody></table></div>
      </>}
  </>;
}

export function OrganizationSettings({ resource, hub, openTab }: { resource: AccessResource; hub: Hub; openTab: OpenTab }) {
  const [name, setName] = useState(resource.name);
  const [destination, setDestination] = useState(resource.parent_id ?? "workloads");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const parent = hub.resources.find((r) => r.id === resource.parent_id);
  const destinations = hub.resources.filter((r) => (r.kind === "root" || r.kind === "folder")
    && !resourceChain(r, hub.resources).some((a) => a.id === resource.id) && r.actions.includes("create"));
  const move = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError(""); setMessage("");
    try { const updated = await api.access.move(resource.id, name.trim(), destination); await hub.refresh(); openTab(resourceTab(updated, "settings")); setMessage("Folder location and name saved."); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!confirm(`Delete the empty folder ${resource.name}?`)) return;
    setBusy(true); setError("");
    try { await api.access.deleteFolder(resource.id); await hub.refresh(); if (parent) openTab(resourceTab(parent)); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <section className="card"><h2>Name &amp; location</h2>
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="success-message" role="status">{message}</p>}
    {parent?.actions.includes("delete") && <form onSubmit={move}>
      <div className="field"><label htmlFor={`move-name-${resource.id}`}>Name</label><input id={`move-name-${resource.id}`} required value={name} onChange={(e) => setName(e.target.value)} /></div>
      <div className="field"><label htmlFor={`move-parent-${resource.id}`}>Folder</label>
        <select id={`move-parent-${resource.id}`} value={destination} onChange={(e) => setDestination(e.target.value)}>
          {!destinations.some((r) => r.id === resource.parent_id) && parent && <option value={parent.id}>{resourcePath(parent, hub.resources)}</option>}
          {destinations.map((r) => <option key={r.id} value={r.id}>{resourcePath(r, hub.resources)}</option>)}
        </select></div>
      <p className="muted">Moving changes inherited access. Direct grants stay with the object. Changing inherited grants requires Owner permission.</p>
      <div className="form-actions"><button className="btn" disabled={busy || !name.trim() || !destination || (name.trim() === resource.name && destination === resource.parent_id)}>Save location &amp; name</button></div>
    </form>}
    {resource.kind === "folder" && resource.actions.includes("delete") && <div className="danger-zone">
      <div><h3>Delete folder</h3><p className="muted">{hub.resources.some((r) => r.parent_id === resource.id) ? "This folder has contents. Move or remove them first; only empty folders can be deleted." : "Only empty folders can be deleted."}</p></div>
      <button className="btn danger" disabled={busy || hub.resources.some((r) => r.parent_id === resource.id)} onClick={remove}>Delete empty folder</button>
    </div>}
  </section>;
}

export function ServicesView({ hub, openTab }: { hub: Hub; openTab: OpenTab }) {
  const [search, setSearch] = useState("");
  const [accounts, setAccounts] = useState<ServiceAccount[]>([]);
  const [showRetired, setShowRetired] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    api.serviceAccounts().then((rows) => { if (!cancelled) setAccounts(rows); })
      .catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);
  const services = hub.resources.filter((r) => r.kind === "service"
    && (r.config.service_account
      ? showRetired || accounts.some((a) => a.id === r.object_id && !a.disabled)
      : hub.workloads.some((p) => p.resource_id === r.id && p.status !== "stopped"))
    && r.name.toLowerCase().includes(search.toLowerCase())).sort((a, b) => a.name.localeCompare(b.name));
  return <><PageHeader icon={<ProductIcon kind="identity" size={22} />} title="Services"
      subtitle="Each workload has a built-in managed identity. Separate service accounts are only needed for external clients or shared identities."
      actions={hub.me?.is_admin && <button className="btn secondary" onClick={() => openTab({ kind: "admin-service-accounts", title: "Service accounts" })}><IconPlus size={14} />Create service account</button>} />
    <div className="object-toolbar"><input type="search" aria-label="Search services" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search services…" />
      {accounts.some((a) => a.disabled) && <label><input type="checkbox" checked={showRetired} onChange={(e) => setShowRetired(e.target.checked)} /> Show retired service accounts</label>}
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <table><thead><tr><th>Service</th><th>Type</th><th>Execution identity</th><th /></tr></thead><tbody>{services.map((r) => {
      const workload = hub.workloads.find((p) => p.resource_id === r.id);
      const account = accounts.find((a) => a.id === r.object_id);
      return <tr key={r.id}>
        <td><button className="text-button object-name" onClick={() => openTab(resourceTab(r))}><ProductIcon kind={r.config.service_account ? "identity" : workload?.kind} runtime={workload?.runtime} size={16} />{r.name}</button>{account?.disabled && <span className="muted"> · retired</span>}</td>
        <td>{r.config.service_account ? "Service account" : workload?.kind ?? "Workload"}</td>
        <td>{r.config.service_account ? "External / shared identity" : workload?.execution_mode === "caller" ? "Caller for data requests" : workload?.run_as ? `Service account: ${workload.run_as}` : "Built-in managed identity"}</td>
        <td className="table-actions"><button className="btn sm secondary" onClick={() => openTab(resourceTab(r, "permissions"))}>Permissions</button></td>
      </tr>;
    })}{!services.length && <tr><td colSpan={4} className="muted">No services match.</td></tr>}</tbody></table>
  </>;
}

export function ServiceView({ tab, hub, openTab }: ObjectProps) {
  const resource = hub.resources.find((r) => r.id === tab.ref && r.config.service_account);
  const [accounts, setAccounts] = useState<ServiceAccount[]>([]);
  const [error, setError] = useState("");
  useEffect(() => { api.serviceAccounts().then(setAccounts).catch((e: Error) => setError(e.message)); }, [tab.ref]);
  if (!resource) return <p className="muted">Service account not available.</p>;
  const name = String(resource.config.account_name);
  const account = accounts.find((a) => a.name === name);
  const section = tab.section ?? "overview";
  return <><nav className="breadcrumbs" aria-label="Object location"><button className="text-button" onClick={() => openTab({ kind: "services", title: "Services" })}>Services</button><span className="breadcrumb-divider" aria-hidden="true">/</span><span aria-current="page">{name}</span></nav>
    <PageHeader icon={<ProductIcon kind="identity" size={22} />} title={name} subtitle={<>Service account{account?.disabled && " · disabled"}{account?.description && ` · ${account.description}`}</>} />
    {resource.layer_name && <p className="hint">Layer · <strong>{resource.layer_name}</strong></p>}
    {resource.owner_group_name && <p className="hint">Owner group · <strong>{resource.owner_group_name}</strong></p>}
    <ObjectTabs active={section} onChange={(s) => openTab({ ...tab, section: s })} />
    {section === "permissions" ? <ObjectAccess resource={resource} hub={hub} openTab={openTab} /> : <>
      <section className="card"><h2>Execution identity</h2>
        <dl className="kv"><dt>Principal</dt><dd className="mono">sa:{name}</dd></dl>
        <p className="muted">Executor grants allow use of this identity. Owner grants also control tokens, secrets, and permission assignment.
          To grant data access, assign a role directly to this service identity on the relevant schema or data source. App-group bindings are also supported.</p>
        <div className="form-actions"><button className="btn secondary" onClick={() => openTab({ kind: "access", title: "App groups" })}>App-group bindings</button></div>
      </section>
      {error && <p className="error" role="alert">{error}</p>}
      {resource.actions.includes("identity") && <TokensCard key={name} sa={name} onError={setError} />}
      <div className="section-heading"><h2>Workloads using this account</h2></div>
      <ul className="object-links">{hub.workloads.filter((p) => p.run_as === name).map((p) => <li key={p.id}>
        <ProductIcon kind={p.kind} runtime={p.runtime} size={16} /><button className="text-button" onClick={() => openTab({ kind: "workload", ref: p.id, title: p.title })}>{p.title}</button>
      </li>)}</ul>
      {!hub.workloads.some((p) => p.run_as === name) && <p className="muted">No visible workloads use this account.</p>}
    </>}
  </>;
}
