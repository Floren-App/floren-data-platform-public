import { objectKindLabel } from "../objectKinds";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { api, AppGroup, CatalogAsset, CatalogGovernance, CatalogSearch, CatalogSuggestion, CatalogSLA } from "../api";
import { CatalogAlerts, MetricsPanel, SlaHistory } from '../components/Metrics';
import type { Hub } from "../hub";
import type { TabRef } from "./Tabs";
import { PermissionsPanel, resourceTab } from "../components/Permissions";
import { ProductIcon } from "../components/ProductIcon";
import { dataTab } from "../components/DataBrowser";
import { IconCatalog, IconRefresh } from "../components/Icons";
import { PageHeader } from "../components/Page";

const LineageExplorer = lazy(() => import("../components/LineageExplorer").then((module) => ({ default: module.LineageExplorer })));

const words = (value: string) => value.replaceAll("_", " ");
const assetTab = (a: CatalogAsset): TabRef => ({ kind: "catalog-object", ref: a.id, title: a.name });
const icon = (a: CatalogAsset) => a.kind.startsWith("airflow") ? "airflow" : a.kind.startsWith("dbt") ? "dbt"
  : a.kind === "root" ? "folder" : a.kind === "schema" ? a.attributes.source_kind === "s3" ? "folder" : "data" : a.kind === "source" ? a.attributes.source_kind : a.kind === "program" ? "python" : a.kind;

function Status({ value }: { value: string }) { return <span className={`badge catalog-status ${value}`}>{words(value)}</span>; }

function Highlight({ text, query }: { text: string; query: string }) {
  const terms = query.match(/[\p{L}\p{N}_-]{2,}/gu)?.filter((v) => !["AND", "OR", "NOT"].includes(v)) ?? [];
  if (!terms.length) return <>{text}</>;
  const pattern = new RegExp(`(${terms.map((v) => v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return <>{text.split(pattern).map((part, i) => i % 2 ? <mark key={i}>{part}</mark> : part)}</>;
}

export function CatalogSearchBox({ value, onChange, openTab, id = "catalog-search", label = "Search the catalog", autoFocus = true, onSubmit }: {
  value: string; onChange: (value: string) => void; openTab: (tab: TabRef) => void;
  id?: string; label?: string; autoFocus?: boolean; onSubmit?: () => void;
}) {
  const [items, setItems] = useState<CatalogSuggestion[]>([]), [open, setOpen] = useState(false), [active, setActive] = useState(-1);
  useEffect(() => {
    let stale = false; setItems([]); setActive(-1);
    if (value.trim().length < 2) return;
    const timer = setTimeout(() => api.catalog.suggest(value).then((result) => { if (!stale) setItems(result.items); }).catch(() => undefined), 150);
    return () => { stale = true; clearTimeout(timer); };
  }, [value]);
  const choose = (item: CatalogSuggestion) => { setOpen(false); openTab({ kind: "catalog-object", ref: item.id, title: item.name }); };
  return <div className="catalog-search"><label htmlFor={id}>{label} <span className="kbd" aria-hidden="true">⌘K / Ctrl+K</span></label>
    <div className="catalog-search-input"><input id={id} aria-label={label} type="search" autoFocus={autoFocus} role="combobox" aria-autocomplete="list" aria-expanded={open && !!items.length} aria-controls={`${id}-suggestions`} aria-activedescendant={active >= 0 && open ? `${id}-suggestion-${active}` : undefined}
      value={value} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onChange={(e) => { onChange(e.target.value); setOpen(true); }} onKeyDown={(e) => {
        if (e.key === "Escape") { setOpen(false); setActive(-1); }
        if ((e.key === "ArrowDown" || e.key === "ArrowUp") && items.length) { e.preventDefault(); setOpen(true); setActive((i) => (i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length); }
        if (e.key === "Enter") { e.preventDefault(); if (open && active >= 0) choose(items[active]); else { setOpen(false); onSubmit?.(); } }
      }} placeholder={'Search products, columns, owners, tags… or domain:Finance type:table'} />
      {open && !!items.length && <div className="catalog-suggestions" role="listbox" id={`${id}-suggestions`} aria-label="Catalog suggestions">{items.map((item, i) => <button key={item.id} role="option" id={`${id}-suggestion-${i}`} aria-selected={active === i} className={active === i ? "active" : ""} onMouseDown={(e) => e.preventDefault()} onClick={() => choose(item)}>
        <ProductIcon kind={item.kind.startsWith("dbt") ? "dbt" : item.kind.startsWith("airflow") ? "airflow" : item.kind} size={16} /><span><strong><Highlight text={item.name} query={value} /></strong><span className="sub">{objectKindLabel(item.kind)} · {item.path}</span></span>{item.approximate && <span className="badge">close match</span>}</button>)}</div>}
    </div></div>;
}

export function CatalogView({ openTab, initialKind = "", initialQuery = "" }: { openTab: (tab: TabRef) => void; initialKind?: string; initialQuery?: string }) {
  const [filters, setFilters] = useState<Record<string, string>>({ q: initialQuery, kind: initialKind, domain: "", branch: "", status: "", classification: "", sla_status: "", layer: "", tag: "", owner: "", platform: "", column: "", sort: "relevance", offset: "0" });
  const [result, setResult] = useState<CatalogSearch | null>(null);
  const [busy, setBusy] = useState(true), [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => { setFilters((f) => ({ ...f, kind: initialKind, q: initialQuery, offset: "0" })); }, [initialKind, initialQuery]);
  useEffect(() => {
    let cancelled = false;
    setBusy(true); setError("");
    const timer = setTimeout(() => api.catalog.search(filters).then((data) => { if (!cancelled) setResult(data); })
      .catch((e: Error) => { if (!cancelled) setError(e.message); }).finally(() => { if (!cancelled) setBusy(false); }), 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [filters, version]);
  const filter = (key: string, value: string) => setFilters((f) => ({ ...f, [key]: value, offset: "0" }));
  return <>
    <PageHeader icon={<IconCatalog size={22} />} title="Catalog" subtitle="Find tables, schemas, storage connections, apps and workloads with their dependencies."
      actions={<button className="btn secondary" onClick={() => setVersion((v) => v + 1)}><IconRefresh size={14} />Refresh catalog</button>} />
    <CatalogSearchBox value={filters.q} onChange={(value) => filter("q", value)} openTab={openTab} />
    <CatalogAlerts key={version} openTab={openTab}/>
    <div className="catalog-filters">
      {([['kind', 'Type', result?.facets.kind ?? []], ['domain', 'Domain', result?.facets.domain ?? []], ['branch', 'Branch', result?.facets.branch ?? []], ['layer', 'Layer', result?.facets.layer ?? []],
        ['platform', 'Platform', result?.facets.platform ?? []], ['owner', 'Owner group', result?.facets.owner ?? []], ['tag', 'Tag', result?.facets.tag ?? []],
        ['status', 'Governance', ['draft', 'reviewed', 'certified', 'deprecated']], ['classification', 'Classification', ['unclassified', 'public', 'internal', 'confidential', 'restricted']],
        ['sla_status', 'SLA', ['not_configured', 'unknown', 'healthy', 'at_risk', 'breached']]] as [string, string, string[]][]).map(([key, label, options]) => <div className="field" key={key}>
          <label htmlFor={`catalog-${key}`}>{label}</label><select id={`catalog-${key}`} value={filters[key]} onChange={(e) => filter(key, e.target.value)}><option value="">All</option>
            {[...new Set([...options, ...(filters[key] ? [filters[key]] : [])])].map((value) => <option key={value} value={value}>{words(value)}</option>)}</select></div>)}
    </div>
    <div className="catalog-filters" style={{ marginTop: 0 }}><div className="field" style={{ gridColumn: "span 2" }}><label htmlFor="catalog-column">Column name</label><input id="catalog-column" value={filters.column} onChange={(e) => filter("column", e.target.value)} placeholder="Find datasets with this column" /></div></div>
    <details className="catalog-search-help"><summary>Search syntax</summary><p>Words match together. Use quotes for phrases, OR for alternatives, and NOT or a minus to exclude. Field filters include name, type, domain, owner, tag, column, platform, branch, layer, path, status, and classification. Use * for a wildcard.</p><code>domain:Finance (type:table OR type:dbt) -status:deprecated</code></details>
    {error && <p role="alert" className="error">{error}</p>}
    <div className="toolbar" style={{ alignItems: "center", borderBottom: "1px solid var(--border)", paddingBottom: 8, marginBottom: 0 }}>
      <div className="toolbar-group"><span className="muted result-count" role="status">{busy ? "Searching…" : `${result?.total ?? 0} objects you can access`}</span>
        {result?.approximate && <span className="muted result-count">· includes close spelling matches; exact matches rank first</span>}</div>
      <div className="toolbar-group">
        <button className="btn sm ghost" disabled={Object.entries(filters).every(([k, v]) => k === "sort" ? v === "relevance" : k === "offset" || !v)}
          onClick={() => setFilters((f) => Object.fromEntries(Object.keys(f).map((k) => [k, k === "sort" ? "relevance" : k === "offset" ? "0" : ""])))}>Clear filters</button>
        <label className="row muted" htmlFor="catalog-sort" style={{ fontSize: 12, gap: 6 }}>Sort results
          <select id="catalog-sort" style={{ height: 22 }} value={filters.sort} onChange={(e) => filter("sort", e.target.value)}><option value="relevance">Relevance</option><option value="name">Name</option><option value="recent">Recently observed</option></select></label>
      </div>
    </div>
    <div className="table-scroll"><table aria-label="Catalog results" aria-busy={busy}><thead><tr><th>Object</th><th>Layer</th><th>Type</th><th>Domain</th><th>Governance</th><th>SLA</th><th /></tr></thead><tbody>
      {result?.items.map((a) => <tr key={a.id}><td><button className="text-button object-name" onClick={() => openTab(assetTab(a))}><ProductIcon kind={icon(a)} runtime={a.attributes.runtime} size={16} /><Highlight text={a.name} query={filters.q} /></button>
        <div className="muted">{a.path}</div>{(a.governance.description || a.attributes.description) && <div className="catalog-excerpt"><Highlight text={a.governance.description || a.attributes.description || ""} query={filters.q} /></div>}
        {!!a.matched_fields?.length && <div className="muted">Matched {a.matched_fields.join(", ")}{a.approximate && " · close spelling"}</div>}
        {a.attributes.branch && <span className="muted mono">{a.attributes.repo} · {a.attributes.branch}</span>}</td>
        <td>{a.layer_name || "—"}</td><td>{objectKindLabel(a.kind, a.attributes.source_kind)}</td><td>{a.governance.domain || "—"}</td><td><Status value={a.governance.status || "draft"} /></td><td><Status value={a.service_level.status} /></td>
        <td className="table-actions"><button className="btn sm secondary" onClick={() => openTab({ ...assetTab(a), catalogSection: "access" })}>Access</button></td></tr>)}
      {!busy && !result?.items.length && <tr><td colSpan={7} className="muted">No accessible objects match these filters.</td></tr>}
    </tbody></table></div>
    {!!result?.total && <div className="row catalog-pagination"><span className="muted">{result.offset + 1}–{Math.min(result.total, result.offset + result.limit)} of {result.total}</span>
      <button className="btn sm secondary" disabled={busy || result.offset === 0} onClick={() => setFilters((f) => ({ ...f, offset: String(Math.max(0, result.offset - result.limit)) }))}>Previous</button>
      <button className="btn sm secondary" disabled={busy || result.offset + result.limit >= result.total} onClick={() => setFilters((f) => ({ ...f, offset: String(result.offset + result.limit) }))}>Next</button></div>}
    <p className="footnote">Tables and files enter the catalog when their metadata is opened or imported from lineage. Search never scans data sources. Uncertified objects remain available under their existing roles.</p>
  </>;
}

export function CatalogObject({ tab, hub, openTab }: { tab: TabRef; hub: Hub; openTab: (tab: TabRef) => void }) {
  const [asset, setAsset] = useState<CatalogAsset | null>(null), [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => { let stale = false; api.catalog.asset(tab.ref!).then((a) => { if (!stale) { setAsset(a); setError(""); } }).catch((e: Error) => { if (!stale) { setAsset(null); setError(e.message); } }); return () => { stale = true; }; }, [tab.ref, version]);
  useEffect(()=>{if(tab.catalogSection!=='sla')return;const timer=setInterval(()=>setVersion(v=>v+1),60_000);return()=>clearInterval(timer);},[tab.ref,tab.catalogSection]);
  if (!asset) return <p className={error ? "error" : "muted"} role={error ? "alert" : undefined}>{error || "Loading catalog object…"}</p>;
  const section = tab.catalogSection ?? "overview";
  const reload = async () => { await hub.refresh(); setVersion((v) => v + 1); };
  const openObject = () => {
    if (asset.attributes.metric_id && asset.attributes.parent_asset_id) {
      openTab({kind:'catalog-object',ref:String(asset.attributes.parent_asset_id),title:asset.name,catalogSection:'metrics'}); return;
    }
    if (["table", "view", "materialized_view", "file", "folder"].includes(asset.kind) && asset.resource.kind === "schema") {
      openTab(dataTab(asset.resource, { name: asset.name, kind: asset.kind as "file" | "folder" | "table" | "view" | "materialized_view", path: asset.attributes.object_path }));
    } else openTab(resourceTab(asset.resource));
  };
  return <>
    <nav className="breadcrumbs" aria-label="Catalog location"><button className="text-button" onClick={() => openTab({ kind: "catalog", title: "Catalog" })}>Catalog</button>
      {asset.path.split(" / ").map((part, i, all) => <span key={i}><span className="breadcrumb-divider" aria-hidden="true">/</span><span aria-current={i === all.length - 1 ? "page" : undefined}>{part}</span></span>)}</nav>
    <PageHeader icon={<ProductIcon kind={icon(asset)} runtime={asset.attributes.runtime} size={22} />} title={asset.name}
      subtitle={<>{objectKindLabel(asset.kind, asset.attributes.source_kind)}{asset.attributes.branch && ` · ${asset.attributes.repo ? asset.attributes.repo + " · " : ""}${asset.attributes.branch}`}</>}
      actions={<button className="btn secondary" onClick={openObject}>Open object</button>}>
      <div className="page-meta">{asset.layer_name && <span className="badge">{asset.layer_name}</span>}<Status value={asset.governance.status || "draft"} /><Status value={asset.governance.classification || "unclassified"} /><Status value={asset.service_level.status} /></div>
    </PageHeader>
    <nav className="object-tabs" aria-label="Catalog sections">{([['overview', 'Overview'], ['lineage', 'Lineage'], ['metrics', 'KPIs & quality'], ['governance', 'Governance'], ['sla', 'SLA'], ['access', 'Permissions']] as const).map(([id, label]) => <button key={id} className={section === id ? "active" : ""} aria-current={section === id ? "page" : undefined} onClick={() => openTab({ ...tab, catalogSection: id })}>{label}</button>)}</nav>
    {error && <p role="alert" className="error">{error}</p>}
    {section === "overview" && <>
      <p className={asset.governance.description || asset.attributes.description ? "" : "muted"} style={{ maxWidth: "90ch", marginTop: 0 }}>{asset.governance.description || asset.attributes.description || "No description yet. Add one in Governance."}</p>
      <section className="card"><dl className="kv"><dt>Domain</dt><dd>{asset.governance.domain || <span className="muted">Unassigned</span>}</dd><dt>Steward</dt><dd>{asset.governance.steward || <span className="muted">Unassigned</span>}</dd>
        {!!asset.governance.tags?.length && <><dt>Tags</dt><dd className="row wrap" style={{ gap: 4 }}>{asset.governance.tags.map((tag) => <span className="chip" key={tag}>{tag}</span>)}</dd></>}
        <dt>IAM boundary</dt><dd>{asset.resource.name}</dd><dt>Metadata observed</dt><dd>{new Date(asset.observed_at).toLocaleString()}</dd></dl></section>
      {!!asset.attributes.columns?.length && <><div className="section-heading" style={{ justifyContent: "flex-start", gap: 8 }}><h2>Columns</h2><span className="badge">{asset.attributes.columns.length}</span></div>
        <table aria-label="Catalog columns"><thead><tr><th>Column</th><th>Type</th><th>Description</th></tr></thead><tbody>{asset.attributes.columns.map((column) => <tr key={column.name}><td className="mono">{column.name}</td><td className="mono muted">{column.data_type}</td><td>{column.description || <span className="muted">—</span>}</td></tr>)}</tbody></table></>}
    </>}
    {section === "access" && <><p className="scope-note">{asset.resource.kind === "schema" ? "This object uses its schema's IAM. Grants cover every current and future object in that schema." : "Access uses the same roles and inherited grants as the deployed object. Owner permission is required to change assignments."}</p><PermissionsPanel resource={asset.resource} resources={hub.resources} openTab={openTab} onChanged={reload} /></>}
    {section === "governance" && <GovernanceEditor key={asset.id} asset={asset} onSaved={setAsset} />}
    {section === "sla" && <SLAEditor key={asset.id} asset={asset} onSaved={setAsset} />}
    {section === "metrics" && <MetricsPanel key={asset.id} asset={asset} />}
    {section === "lineage" && <><Suspense fallback={<p className="muted">Loading lineage explorer…</p>}><LineageExplorer asset={asset} openTab={openTab} version={version} /></Suspense><NativeImport asset={asset} onImported={() => setVersion((v) => v + 1)} /></>}
  </>;
}

function GovernanceEditor({ asset, onSaved }: { asset: CatalogAsset; onSaved: (asset: CatalogAsset) => void }) {
  const [form, setForm] = useState<CatalogGovernance>({ description: asset.attributes.description || "", domain: "", owner_group_id: null, steward: "", classification: "unclassified", tags: [], status: "draft", lineage_role: "data", ...asset.governance });
  const [groups, setGroups] = useState<AppGroup[]>([]), [error, setError] = useState(""), [message, setMessage] = useState(""), [busy, setBusy] = useState(false);
  const [tags, setTags] = useState(form.tags?.join(", ") || "");
  useEffect(() => { api.access.groups().then(setGroups).catch((e: Error) => setError(e.message)); }, []);
  const save = async (event: React.FormEvent) => { event.preventDefault(); setBusy(true); setError(""); setMessage(""); try { const updated = await api.catalog.governance(asset.id, { ...form, tags: tags.split(",").map((v) => v.trim()).filter(Boolean) }); onSaved(updated); setForm({ ...form, ...updated.governance }); setMessage("Governance saved."); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const edit = asset.can_edit || asset.can_govern;
  return <form className="card" onSubmit={save}><h2>Governance</h2><p className="muted">Owners manage accountability, classification, and certification. Contributors can document products. Certification is visible and does not block deploying. Material documentation edits return a certified object to review.</p>
    <div className="field"><label htmlFor="gov-description">Description</label><textarea id="gov-description" rows={4} maxLength={10000} disabled={!edit} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></div>
    <div className="arg-grid"><div className="field"><label htmlFor="gov-domain">Domain</label><input id="gov-domain" maxLength={128} disabled={!edit} value={form.domain} onChange={(e) => setForm({ ...form, domain: e.target.value })} /></div>
      <div className="field"><label htmlFor="gov-tags">Tags (comma separated)</label><input id="gov-tags" disabled={!edit} value={tags} onChange={(e) => setTags(e.target.value)} /></div>
      <div className="field"><label htmlFor="gov-owner">Accountable owner group</label><select id="gov-owner" disabled={!asset.can_govern} value={form.owner_group_id || ""} onChange={(e) => setForm({ ...form, owner_group_id: e.target.value || null })}><option value="">Unassigned</option>{form.owner_group_id && !groups.some((g) => g.id === form.owner_group_id) && <option value={form.owner_group_id}>{asset.owner_group_name || "Accountable group"}</option>}{groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select><span className="hint">Accountability metadata does not grant IAM access.</span></div>
      <div className="field"><label htmlFor="gov-steward">Steward / contact</label><input id="gov-steward" disabled={!asset.can_govern} maxLength={256} value={form.steward} onChange={(e) => setForm({ ...form, steward: e.target.value })} /></div>
      <div className="field"><label htmlFor="gov-classification">Classification</label><select id="gov-classification" disabled={!asset.can_govern} value={form.classification} onChange={(e) => setForm({ ...form, classification: e.target.value })}>{['unclassified', 'public', 'internal', 'confidential', 'restricted'].map((v) => <option key={v}>{v}</option>)}</select></div>
      <div className="field"><label htmlFor="gov-lineage-role">Lineage purpose</label><select id="gov-lineage-role" disabled={!asset.can_govern} value={form.lineage_role || "data"} onChange={(e) => setForm({ ...form, lineage_role: e.target.value as "data" | "operational" })}><option value="data">Data</option><option value="operational">Operational metadata</option></select><span className="hint">Checkpoints and process-history tables appear as operational dependencies, separate from the main data flow.</span></div>
      <div className="field"><label htmlFor="gov-status">Governance status</label><select id="gov-status" disabled={!asset.can_govern} value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>{['draft', 'reviewed', 'certified', 'deprecated'].map((v) => <option key={v}>{v}</option>)}</select></div></div>
    {error && <p role="alert" className="error">{error}</p>}{message && <p className="success-message" role="status">{message}</p>}{edit && <div className="form-actions"><button className="btn" disabled={busy}>Save governance</button></div>}
  </form>;
}

function SLAEditor({ asset, onSaved }: { asset: CatalogAsset; onSaved: (asset: CatalogAsset) => void }) {
  const [freshness, setFreshness] = useState(asset.sla.freshness_minutes?.toString() || ""), [rate, setRate] = useState(asset.sla.success_rate_percent?.toString() || ""), [windowHours, setWindowHours] = useState(String(asset.sla.window_hours || 168));
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [policy,setPolicy]=useState<CatalogSLA>({warning_percent:asset.sla.warning_percent??80,freshness_basis:asset.sla.freshness_basis||'refresh',deadline_time:asset.sla.deadline_time??null,timezone:asset.sla.timezone||'UTC',weekdays:asset.sla.weekdays||[0,1,2,3,4,5,6],grace_minutes:asset.sla.grace_minutes??15,warning_minutes:asset.sla.warning_minutes??30,quality_required:asset.sla.quality_required||false,quality_max_age_minutes:asset.sla.quality_max_age_minutes??1440,required_assets:asset.sla.required_assets||[],enforcement:asset.sla.enforcement||'monitor',block_on_unknown:asset.sla.block_on_unknown??true});
  const [required,setRequired]=useState((asset.sla.required_assets||[]).join(', '));
  const save = async (e: React.FormEvent) => { e.preventDefault(); setBusy(true); setError(""); try { onSaved(await api.catalog.sla(asset.id, { ...policy,required_assets:required.split(',').map(v=>v.trim()).filter(Boolean), freshness_minutes: freshness ? Number(freshness) : null, success_rate_percent: rate ? Number(rate) : null, window_hours: Number(windowHours) })); setMessage("SLA saved."); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  return <><section className="card"><h2 className="row">Service level <Status value={asset.service_level.status} /></h2><p className="muted">Freshness measures the last successful data refresh. Successful-run rate uses finished runs in the configured window; stopped runs count against the target. Docs generation and tests do not count as refreshes. Missing observations are shown as unknown.</p>
    {!!asset.service_level.checks.length && <table aria-label="SLA checks"><thead><tr><th>Check</th><th>Observed</th><th>Target</th><th>Status</th></tr></thead><tbody>{asset.service_level.checks.map((check) => <tr key={check.name}><td>{check.name}{check.samples !== undefined && ` (${check.samples} runs)`}</td><td>{check.value ?? <span className="muted">Unknown</span>} {check.value !== null && check.unit}</td><td>{check.target} {check.unit}</td><td><Status value={check.status} /></td></tr>)}</tbody></table>}
    <p className="muted">Last successful refresh: {asset.service_level.last_success_at ? new Date(asset.service_level.last_success_at).toLocaleString() : "No observation yet"}</p></section>
    <form className="card" onSubmit={save}><h2>SLA policy</h2><p className="muted">Only Owners can change commitments. Leave a target empty to disable that check.</p><div className="arg-grid">
      <div className="field"><label htmlFor="sla-freshness">Maximum data age (minutes)</label><input id="sla-freshness" type="number" min={1} max={525600} disabled={!asset.can_govern} value={freshness} onChange={(e) => setFreshness(e.target.value)} /></div>
      <div className="field"><label htmlFor="sla-basis">Freshness evidence</label><select id="sla-basis" disabled={!asset.can_govern} value={policy.freshness_basis} onChange={e=>setPolicy({...policy,freshness_basis:e.target.value as CatalogSLA['freshness_basis']})}><option value="refresh">Successful materialization</option><option value="checked">Successful source check</option><option value="watermark">Source data timestamp</option></select></div>
      <div className="field"><label htmlFor="sla-warning">Warn at age budget (%)</label><input id="sla-warning" type="number" min={1} max={99} disabled={!asset.can_govern} value={policy.warning_percent} onChange={e=>setPolicy({...policy,warning_percent:Number(e.target.value)})}/></div>
      {asset.resource.kind === "service" && <><div className="field"><label htmlFor="sla-rate">Minimum successful runs (%)</label><input id="sla-rate" type="number" min={0} max={100} step="0.1" disabled={!asset.can_govern} value={rate} onChange={(e) => setRate(e.target.value)} /></div>
        <div className="field"><label htmlFor="sla-window">Run window (hours)</label><input id="sla-window" type="number" min={1} max={8760} required disabled={!asset.can_govern} value={windowHours} onChange={(e) => setWindowHours(e.target.value)} /></div></>}</div>
      <h3>Scheduled refresh</h3><div className="arg-grid">
        <div className="field"><label htmlFor="sla-deadline">Daily deadline</label><input id="sla-deadline" type="time" disabled={!asset.can_govern} value={policy.deadline_time||''} onChange={e=>setPolicy({...policy,deadline_time:e.target.value||null})}/></div>
        <div className="field"><label htmlFor="sla-timezone">Timezone</label><input id="sla-timezone" disabled={!asset.can_govern} value={policy.timezone} placeholder="Europe/Amsterdam" onChange={e=>setPolicy({...policy,timezone:e.target.value})}/></div>
        <div className="field"><label htmlFor="sla-grace">Grace period (minutes)</label><input id="sla-grace" type="number" min={0} max={1440} disabled={!asset.can_govern} value={policy.grace_minutes} onChange={e=>setPolicy({...policy,grace_minutes:Number(e.target.value)})}/></div>
        <div className="field"><label htmlFor="sla-before">Warn before deadline (minutes)</label><input id="sla-before" type="number" min={0} max={1440} disabled={!asset.can_govern} value={policy.warning_minutes} onChange={e=>setPolicy({...policy,warning_minutes:Number(e.target.value)})}/></div>
      </div><div className="row wrap" role="group" aria-label="Deadline weekdays">{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((day,i)=><label className="row" key={day}><input type="checkbox" disabled={!asset.can_govern} checked={policy.weekdays?.includes(i)} onChange={e=>setPolicy({...policy,weekdays:e.target.checked?[...(policy.weekdays||[]),i]:(policy.weekdays||[]).filter(d=>d!==i)})}/>{day}</label>)}</div>
      <h3>Data quality</h3><label className="row"><input type="checkbox" disabled={!asset.can_govern} checked={policy.quality_required} onChange={e=>setPolicy({...policy,quality_required:e.target.checked})}/>Require current passing quality checks</label>
      <div className="field"><label htmlFor="sla-quality-age">Maximum quality result age (minutes)</label><input id="sla-quality-age" type="number" min={1} disabled={!asset.can_govern} value={policy.quality_max_age_minutes} onChange={e=>setPolicy({...policy,quality_max_age_minutes:Number(e.target.value)})}/></div>
      {asset.resource.kind==='service'&&<><h3>Upstream input gate</h3><p className="muted">Only these explicitly selected inputs can block new runs. Running jobs and data repair producers continue normally.</p>
        <div className="field"><label htmlFor="sla-inputs">Required upstream catalog IDs</label><input id="sla-inputs" disabled={!asset.can_govern} value={required} onChange={e=>setRequired(e.target.value)}/></div>
        <div className="field"><label htmlFor="sla-enforcement">Enforcement</label><select id="sla-enforcement" disabled={!asset.can_govern} value={policy.enforcement} onChange={e=>setPolicy({...policy,enforcement:e.target.value as CatalogSLA['enforcement']})}><option value="monitor">Monitor only</option><option value="block_new_runs">Block new runs on breached inputs</option></select></div>
        <label className="row"><input type="checkbox" disabled={!asset.can_govern} checked={policy.block_on_unknown} onChange={e=>setPolicy({...policy,block_on_unknown:e.target.checked})}/>Also block when required input status is unknown</label></>}
      {error && <p className="error" role="alert">{error}</p>}{message && <p className="success-message" role="status">{message}</p>}{asset.can_govern && <div className="form-actions"><button className="btn" disabled={busy}>Save SLA</button></div>}</form><SlaHistory asset={asset} onEvaluated={async()=>onSaved(await api.catalog.asset(asset.id))}/></>;
}

function NativeImport({ asset, onImported }: { asset: CatalogAsset; onImported: () => void }) {
  const [error, setError] = useState(""), [message, setMessage] = useState(""), [busy, setBusy] = useState(false);
  const files = useRef<Record<string, unknown>>({});
  const workload = asset.resource.kind === "service" && ["dbt", "airflow"].includes(asset.kind);
  if (!workload) return null;
  const importFiles = async (e: React.FormEvent) => { e.preventDefault(); setBusy(true); setError(""); setMessage(""); try { const bundle = files.current; const payload = asset.kind === "dbt" ? { manifest: bundle["manifest.json"], catalog: bundle["catalog.json"], run_results: bundle["run_results.json"], sources: bundle["sources.json"] } : Object.values(bundle)[0]; await api.catalog.import(asset.resource.id, asset.kind as "dbt" | "airflow", payload); setMessage("Native lineage imported."); onImported(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  return <section className="card"><h2>Native lineage imports</h2><p className="muted">New dbt runs upload manifest, catalog, run results, and freshness artifacts automatically. Airflow deployments report native task dependencies and rendered inlets/outlets through the SDK lineage backend. Runtime images need the current SDK. Imports describe dependencies and do not grant access.</p>
    {asset.imports?.length ? <ul>{asset.imports.map((i) => <li key={`${i.origin}:${i.scope}`}>{words(i.origin)} {i.scope} · {i.nodes} nodes · {new Date(i.observed_at).toLocaleString()}</li>)}</ul> : <p className="muted">No native metadata received yet.</p>}
    {asset.resource.actions.includes("deploy") && <form onSubmit={importFiles} className="toolbar" style={{ justifyContent: "flex-start" }}><div className="field"><label htmlFor="lineage-files">{asset.kind === "dbt" ? "manifest.json (required), catalog.json, run_results.json, sources.json" : "Airflow DAG metadata JSON"}</label><input id="lineage-files" type="file" accept="application/json,.json" multiple={asset.kind === "dbt"} onChange={async (e) => { files.current = {}; setError(""); try { for (const f of Array.from(e.target.files || [])) { if (f.size > 8 * 1024 * 1024) throw new Error("Each artifact must be smaller than 8 MB."); files.current[f.name] = JSON.parse(await f.text()); } } catch (e) { setError((e as Error).message); files.current = {}; } }} /></div><button className="btn secondary" disabled={busy} style={{ marginBottom: 12 }}>Import native metadata</button></form>}
    {error && <p role="alert" className="error">{error}</p>}{message && <p className="success-message" role="status">{message}</p>}
  </section>;
}
