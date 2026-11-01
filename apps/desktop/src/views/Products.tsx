import { useCallback, useEffect, useState } from "react";
import { AccessCaseItem, AccessRequestItem, ago, api, ApiError, openInWindow, AppGroup, CaseTarget, JustifyDeadline, Requirement, AssetProduct, OptionInput, Product, ProductCandidate, ProductDetail, ProductGrant, ProductMember, ProductOpen, ProductStep } from "../api";
import type { Hub } from "../hub";
import { PageHeader } from "../components/Page";
import { ProductIcon } from "../components/ProductIcon";
import { PermissionsPanel } from "../components/Permissions";
import { Answers, FormRenderer, JustifyPanel, uploadFiles } from "../components/ComplianceForm";
import { CaseTable } from "./Cases";
import { AlertRules } from "./Alerts";
import { IconCatalog, IconPlus } from "../components/Icons";
import { objectKindLabel } from "../objectKinds";
import type { TabRef } from "./Tabs";

type OpenTab = (tab: TabRef) => void;

export const productTab = (p: Pick<Product, "id" | "title">, extra: Partial<TabRef> = {}): TabRef => ({ kind: "product", ref: p.id, title: p.title, ...extra });
const KIND: Record<string, [string, string]> = { app: ["Dashboard", "dashboards"], table: ["Table", "tables"], view: ["View", "views"],
  materialized_view: ["Materialized view", "materialized views"], api: ["API", "APIs"], topic: ["Kafka topic", "Kafka topics"] };
const kindLabel = (kind: string) => KIND[kind]?.[0] ?? objectKindLabel(kind);
const words = (value: string) => value.replaceAll("_", " ");

/** "Dashboard · 2 tables": what is in a product, by kind, lead first. */
export function contents(members: Pick<ProductMember, "kind">[]): string {
  const counts = new Map<string, number>();
  for (const m of members) counts.set(m.kind, (counts.get(m.kind) ?? 0) + 1);
  return [...counts].map(([kind, n]) => n === 1 ? kindLabel(kind) : `${n} ${KIND[kind]?.[1] ?? kindLabel(kind).toLowerCase() + "s"}`).join(" · ");
}

/** What Consumer on the product gives, in words: "Executor on Sales app, Reader on schema public (all its tables)". */
function grantsText(grants: ProductGrant[]) {
  return grants.map((g, i) => <span key={g.resource_id}>{i > 0 && ", "}{g.role} on {g.kind === "schema" ? <>schema <strong>{g.name}</strong> (all its tables)</> : <strong>{g.name}</strong>}</span>);
}

/** Where to open an object itself: the dashboard, the table, the API or the topic (only with access). */
export function openTarget(o: ProductOpen | null, name: string, kind: string): TabRef | null {
  if (!o) return null;
  if (o.resource_kind === "service" && o.object_id) return { kind: "workload", ref: o.object_id, title: name };
  if (o.resource_kind === "schema" && o.object_path) return { kind: "data-object", ref: JSON.stringify([o.resource_id, kind, o.object_path]), title: o.object_path,
    data: { schemaId: o.resource_id, table: o.object_path, item: { name: o.object_path, kind: kind as "table" } } };
  if (o.resource_kind === "api" && o.object_id) return { kind: "api", ref: o.object_id, title: name };
  if (o.resource_kind === "topic") return { kind: "kafka", kafkaKind: "topic", ref: o.resource_id, title: name };
  return null;
}

function BigIcon({ p, size = 30 }: { p: Pick<Product, "kind" | "runtime">; size?: number }) {
  return <span className={`product-mark kind-${p.kind}`} aria-hidden="true"><ProductIcon kind={p.kind} runtime={p.runtime ?? undefined} size={size} /></span>;
}

/** Opens a dashboard: its workload page for people with Workloads, otherwise the app itself. */
function OpenDashboard({ p, hub, openTab, small = false }: { p: Product; hub: Hub; openTab: OpenTab; small?: boolean }) {
  const target = openTarget(p.open, p.name, p.kind);
  const id = p.open?.object_id;
  if (!target || !id) return null;
  return <button className={`btn ${small ? "sm" : "secondary"}`} onClick={() => hub.has("workloads.view") ? openTab(target)
    : void hub.act(() => openInWindow(api.openWorkload(id).then((r) => r.url), p.title, `pub-${id}`))}>Open dashboard</button>;
}

function AccessState({ p }: { p: Product }) {
  return p.access === "granted" ? <span className="product-chip ok">You have access</span>
    : p.access === "requested" ? <span className="product-chip wait">Requested</span>
    : p.access === "partial" ? <span className="product-chip wait" title="You can use some of what is in it">Partly</span>
    : <span className="product-chip">No access yet</span>;
}

export function ProductCard({ p, openTab, hub }: { p: Product; openTab: OpenTab; hub: Hub }) {
  return <article className="product-card" aria-label={p.title}>
    <BigIcon p={p} />
    <div className="product-card-head">
      <button className="text-button product-title" onClick={() => openTab(productTab(p))}>{p.title}</button>
      <span className="muted">{p.owner_group_name ?? "No owner group"}{p.domain && ` · ${p.domain}`}</span>
    </div>
    <p className="product-summary">{p.summary || <span className="muted">No summary yet.</span>}</p>
    <div className="product-card-foot">
      <span className="product-contents" title={p.members.map((m) => `${kindLabel(m.kind)}: ${m.name}`).join("\n")}>
        {p.members.slice(0, 4).map((m) => <ProductIcon key={m.asset_id} kind={m.kind} runtime={m.runtime ?? undefined} size={14} />)}{contents(p.members)}</span>
      {p.status === "certified" && <span className="product-chip cert">Certified</span>}
      {p.updated_at && <span className="product-chip" title={new Date(p.updated_at).toLocaleString()}>Updated {ago(p.updated_at)}</span>}
      <span className="grow" />
      {(p.access === "none" || p.access === "partial") && hub.has("products.request") ? <button className="btn sm" onClick={() => openTab(productTab(p, { intent: "request" }))}>Request access</button>
        : p.access === "granted" && p.kind === "app" && p.open ? <OpenDashboard p={p} hub={hub} openTab={openTab} small /> : <AccessState p={p} />}
    </div>
  </article>;
}

/** The catalog's front page: published data products as cards, in the layer mode. */
export function ProductsView({ hub, openTab, tab }: { hub: Hub; openTab: OpenTab; tab: TabRef }) {
  const mine = tab.ref === "mine";
  const [q, setQ] = useState(""), [domain, setDomain] = useState(""), [kind, setKind] = useState(""), [accessible, setAccessible] = useState(false);
  const [data, setData] = useState<{ items: Product[]; domains: string[]; kinds: string[] } | null>(null), [error, setError] = useState("");
  const [publishing, setPublishing] = useState(tab.intent === "publish");
  const [version, setVersion] = useState(0);
  useEffect(() => { if (tab.intent === "publish") setPublishing(true); }, [tab.intent, tab.assetId]);
  useEffect(() => {
    let alive = true;
    const t = setTimeout(() => api.products.list({ layer_id: hub.layerId, q, domain, kind, accessible, mine })
      .then((d) => { if (alive) { setData(d); setError(""); } }).catch((e: Error) => alive && setError(e.message)), 200);
    return () => { alive = false; clearTimeout(t); };
  }, [hub.layerId, q, domain, kind, accessible, mine, version]);
  const layer = hub.layers.find((l) => l.id === hub.layerId)?.name;
  return <>
    <PageHeader icon={<IconCatalog size={22} />} title={mine ? "My products" : "Data products"}
      subtitle={mine ? "Products you own. Change what is in them, who has access, or unpublish them."
        : `Data products shared in ${layer ?? "this layer"}. Open one to see where it comes from and what uses it; request access to the ones you need.`}
      actions={hub.has("products.publish") && <button className="btn" aria-expanded={publishing} onClick={() => setPublishing(!publishing)}><IconPlus size={14} />Publish a product</button>} />
    {publishing && <PublishPanel hub={hub} assetId={tab.assetId} onClose={() => setPublishing(false)}
      onPublished={(p) => { setPublishing(false); setVersion((v) => v + 1); openTab(productTab(p)); }} />}
    <div className="object-toolbar product-filters">
      <input type="search" aria-label="Search data products" placeholder="Search products, owners, domains, tables…" value={q} onChange={(e) => setQ(e.target.value)} />
      <select aria-label="Domain" value={domain} onChange={(e) => setDomain(e.target.value)}><option value="">All domains</option>{data?.domains.map((d) => <option key={d}>{d}</option>)}</select>
      <select aria-label="Contains" value={kind} onChange={(e) => setKind(e.target.value)}><option value="">Any contents</option>{data?.kinds.map((k) => <option key={k} value={k}>With {KIND[k]?.[1] ?? kindLabel(k)}</option>)}</select>
      {!mine && <label className="row muted"><input type="checkbox" checked={accessible} onChange={(e) => setAccessible(e.target.checked)} />Only what I can use</label>}
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {!data ? <p className="muted" role="status">Loading data products…</p>
      : !data.items.length ? <div className="empty-state"><p>{q || domain || kind || accessible ? "No products match." : mine ? "You do not own a product in this layer yet."
          : "Nothing is published in this layer yet."}</p><p className="muted">A product bundles what you share: for example a dashboard with the tables it uses. Publish from an object's page, or with Publish a product.</p></div>
      : <div className="product-grid">{data.items.map((p) => <ProductCard key={p.id} p={p} openTab={openTab} hub={hub} />)}</div>}
  </>;
}

/** Choose objects for a product: what you own in this layer, with what the chosen ones use offered first. */
function MemberPicker({ hub, chosen, onChange, start }: { hub: Hub; chosen: string[]; onChange: (ids: string[]) => void; start?: string }) {
  const [candidates, setCandidates] = useState<ProductCandidate[] | null>(null), [error, setError] = useState(""), [filter, setFilter] = useState("");
  const [suggested, setSuggested] = useState<string[]>([]);
  useEffect(() => { api.products.candidates(hub.layerId).then(setCandidates).catch((e: Error) => setError(e.message)); }, [hub.layerId]);
  useEffect(() => {  // starting from an object: offer (and tick) what it uses directly
    if (!start) return;
    api.products.forAsset(start).then((info) => { const uses = info.uses.map((u) => u.asset_id); setSuggested(uses);
      onChange([...new Set([start, ...chosen, ...uses])]); }).catch(() => undefined);
  }, [start]); // eslint-disable-line react-hooks/exhaustive-deps
  const byId = new Map((candidates ?? []).map((c) => [c.asset_id, c]));
  const shown = (candidates ?? []).filter((c) => chosen.includes(c.asset_id) || suggested.includes(c.asset_id) || (filter && `${c.name} ${c.path}`.toLowerCase().includes(filter.toLowerCase())));
  const order = [...shown].sort((a, b) => Number(!chosen.includes(a.asset_id)) - Number(!chosen.includes(b.asset_id)));
  const toggle = (id: string) => onChange(chosen.includes(id) ? chosen.filter((x) => x !== id) : [...chosen, id]);
  return <fieldset className="publish-members"><legend>What is in it</legend>
    <p className="hint">The first one leads: it sets the card's icon. Use ↑ to make another one lead.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <ul>{order.map((c) => { const i = chosen.indexOf(c.asset_id); return <li key={c.asset_id}>
      <label className="row"><input type="checkbox" checked={i >= 0} onChange={() => toggle(c.asset_id)} /><ProductIcon kind={c.kind} size={16} />
        <span>{c.name}</span><span className="muted">{kindLabel(c.kind)}{c.path ? ` · ${c.path}` : ""}{suggested.includes(c.asset_id) ? " · used by it" : ""}</span></label>
      {i === 0 ? <span className="product-chip">Leads</span> : i > 0 && <button type="button" className="text-button" aria-label={`Make ${c.name} lead`} onClick={() => onChange([c.asset_id, ...chosen.filter((x) => x !== c.asset_id)])}>↑</button>}
    </li>; })}
      {chosen.filter((id) => !byId.has(id)).map((id) => <li key={id} className="muted">An object you no longer own <button type="button" className="text-button" onClick={() => toggle(id)}>Remove</button></li>)}
    </ul>
    <input type="search" aria-label="Find objects to add" placeholder={candidates ? "Find a table, dashboard, API or topic you own…" : "Loading…"} value={filter} onChange={(e) => setFilter(e.target.value)} />
  </fieldset>;
}

/** Publish: a name, a summary, and the objects in it (one or more you own). */
export function PublishPanel({ hub, assetId, onPublished, onClose }: { hub: Hub; assetId?: string; onPublished: (p: Product) => void; onClose: () => void }) {
  const [chosen, setChosen] = useState<string[]>(assetId ? [assetId] : []), [lead, setLead] = useState<AssetProduct | null>(null);
  const [title, setTitle] = useState(""), [summary, setSummary] = useState(""), [domain, setDomain] = useState(""), [classification, setClassification] = useState("internal");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  useEffect(() => {
    if (!chosen[0]) { setLead(null); return; }
    api.products.forAsset(chosen[0]).then((i) => { setLead(i); setTitle((t) => t || i.name); setDomain((d) => d || i.domain);
      if (i.classification !== "unclassified") setClassification(i.classification); }).catch((e: Error) => setError(e.message));
  }, [chosen[0]]); // eslint-disable-line react-hooks/exhaustive-deps
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try { onPublished(await api.products.publish({ asset_ids: chosen, title: title.trim(), summary, domain, classification })); }
    catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  const leads = lead?.products.find((p) => p.asset_id === chosen[0]);
  return <form className="card publish-panel" aria-label="Publish a data product" onSubmit={(e) => void submit(e)}>
    <h2>Publish a data product</h2>
    <p className="muted">Everyone in this layer will see its card: name, summary, owner, domain, what is in it and how fresh it is. Details stay behind permissions; people ask your group for access, which gives them the use of everything in it.</p>
    <div className="arg-grid">
      <div className="field"><label htmlFor="publish-title">Name in the catalog</label><input id="publish-title" maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Pokémon cards" /></div>
      <div className="field"><label htmlFor="publish-domain">Domain</label><input id="publish-domain" maxLength={80} value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="Games" /></div>
      <div className="field" style={{ gridColumn: "1 / -1" }}><label htmlFor="publish-summary">Summary</label>
        <textarea id="publish-summary" rows={2} maxLength={600} value={summary} onChange={(e) => setSummary(e.target.value)} placeholder="What it shows or contains, for whom, and how often it updates." /></div>
      <div className="field"><label htmlFor="publish-classification">Classification</label>
        <select id="publish-classification" value={classification} onChange={(e) => setClassification(e.target.value)}>
          {["public", "internal", "confidential", "restricted"].map((c) => <option key={c}>{c}</option>)}</select></div>
    </div>
    <MemberPicker hub={hub} chosen={chosen} onChange={setChosen} start={assetId} />
    {leads && <p className="hint">{lead?.name} already leads <strong>{leads.title}</strong>: add to that product, or let another object lead.</p>}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="form-actions"><button className="btn" disabled={busy || !chosen.length || !title.trim() || !!leads}>{busy ? "Publishing…" : "Publish"}</button>
      <button type="button" className="btn secondary" onClick={onClose}>Cancel</button></div>
  </form>;
}

/** The lineage of a product: from its sources (left) to what is in it (right). Steps you cannot open are dashed. */
function Lineage({ detail, openTab }: { detail: ProductDetail; openTab: OpenTab }) {
  const depth = Math.max(0, ...detail.lineage.map((s) => s.depth));
  const columns: ProductStep[][] = Array.from({ length: depth }, (_, i) => detail.lineage.filter((s) => s.depth === depth - i));
  if (!detail.lineage.length) return <p className="muted">No lineage recorded yet. It appears when the platform sees what produces this product: dbt and Airflow imports, and the tables workloads read and write.</p>;
  return <div className="table-scroll"><div className="product-lineage" role="list" aria-label="Lineage">
    {columns.map((column, i) => <div key={i} className="product-lineage-column" role="presentation">
      {column.map((s) => <div key={s.key} role="listitem" className={`product-step ${s.visible ? "" : "hidden"}`} title={s.visible ? undefined : "You cannot open this step: only its name, kind and owner are shown."}>
        <span className="row"><ProductIcon kind={s.kind} runtime={s.runtime ?? undefined} size={16} />
          {s.product_id ? <button className="text-button" onClick={() => openTab({ kind: "product", ref: s.product_id!, title: s.name })}>{s.name}</button>
            : s.asset_id ? <button className="text-button" onClick={() => openTab({ kind: "catalog-object", ref: s.asset_id!, title: s.name })}>{s.name}</button> : <span>{s.name}</span>}</span>
        <small className="muted">{kindLabel(s.kind)}{s.owner_group_name ? ` · ${s.owner_group_name}` : ""}{s.product_id ? " · in another product" : ""}</small>
      </div>)}
    </div>)}
    <div className="product-lineage-column product-lineage-members" role="presentation" aria-label="In this product">
      {detail.members.map((m) => <div key={m.asset_id} role="listitem" className="product-step current">
        <span className="row"><ProductIcon kind={m.kind} runtime={m.runtime ?? undefined} size={16} /><strong>{m.name}</strong></span>
        <small className="muted">{kindLabel(m.kind)} · in this product</small></div>)}
    </div>
  </div></div>;
}

function RequestForm({ p, hub, onDone, onCancel }: { p: Product; hub: Hub; onDone: () => void; onCancel: () => void }) {
  const groups = hub.me?.available_groups ?? [];
  const [group, setGroup] = useState(hub.me?.active_group_id && groups.some((g) => g.id === hub.me?.active_group_id) ? hub.me.active_group_id : groups[0]?.id ?? "");
  const [reason, setReason] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [option, setOption] = useState(((p.options ?? []).find((o) => o.default) ?? (p.options ?? [])[0])?.id ?? "");
  // Data under an access policy: the request carries the policy's compliance form.
  const [requirement, setRequirement] = useState<Requirement | null>(null);
  const [answers, setAnswers] = useState<Answers>({}), [files, setFiles] = useState<Record<string, File>>({});
  const [until, setUntil] = useState(""), [complete, setComplete] = useState(true);
  useEffect(() => {
    if (!group) return;
    let live = true;
    api.compliance.requirement({ kind: "product", app_group_id: group, product_id: p.id, option_id: option || undefined })
      .then((r) => { if (live) setRequirement(r.required ? r : null); }).catch(() => { if (live) setRequirement(null); });
    return () => { live = false; };
  }, [p.id, group, option]);
  const definition = requirement?.form?.definition;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      if (!requirement) { await api.products.requestAccess(p.id, group, reason.trim(), option || undefined); onDone(); return; }
      const hasFiles = Object.keys(files).length > 0;
      const r = await api.products.requestAccess(p.id, group, reason.trim(), option || undefined, { answers, until: until || null, submit: !hasFiles });
      if (hasFiles && r.case_id) { await uploadFiles(r.case_id, files); await api.compliance.submitCase(r.case_id); }
      onDone();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  return <form className="card" aria-label={`Request access to ${p.title}`} onSubmit={(e) => void submit(e)}>
    <h2>Request access</h2>
    <p className="muted">{p.owner_group_name ?? "The owners"} decide. Approval makes the group you choose a Consumer of this product: {grantsText(p.grants)}.</p>
    <div className="arg-grid">
      <div className="field"><label htmlFor="request-group">For your group</label>
        <select id="request-group" value={group} onChange={(e) => setGroup(e.target.value)}>{groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select></div>
      {(p.options ?? []).length > 1 && <fieldset className="field request-options" style={{ gridColumn: "1 / -1" }}><legend>Access option</legend>
        {(p.options ?? []).map((o) => <label key={o.id} className="row"><input type="radio" name="request-option" checked={option === o.id} onChange={() => setOption(o.id)} />
          <span><strong>{o.name}</strong>{o.default && <span className="muted"> · default</span>} <span className="product-chip">{words(o.classification)}</span>
            {o.description && <span className="muted"> · {o.description}</span>}</span></label>)}
      </fieldset>}
      <div className="field" style={{ gridColumn: "1 / -1" }}><label htmlFor="request-reason">Why you need it</label>
        <textarea id="request-reason" rows={2} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="What you will use it for" /></div>
    </div>
    {requirement && <div className="justify-panel">
      <h3>{requirement.form?.name ?? "Compliance form"}</h3>
      <p className="muted">With this option the data is <strong>{words(requirement.classification)}</strong>, so the request needs this form. After the owners, the privacy reviewers decide.
        {requirement.max_days ? ` Access lasts at most ${requirement.max_days} days.` : ""}</p>
      {definition ? <FormRenderer definition={definition} answers={answers} onChange={setAnswers} files={files} onComplete={setComplete}
        onFile={(f, file) => setFiles((all) => { const next = { ...all }; if (file) next[f] = file; else delete next[f]; return next; })} />
        : <p className="error" role="alert">The policy's form has no published version yet. Ask the privacy team.</p>}
      <div className="field"><label htmlFor="request-until">Access needed until (optional)</label><input id="request-until" type="date" value={until} onChange={(e) => setUntil(e.target.value)} /></div>
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="form-actions"><button className="btn" disabled={busy || !group || reason.trim().length < 3 || (!!requirement && (!definition || !complete))}>{busy ? "Sending…" : "Send request"}</button>
      <button type="button" className="btn secondary" onClick={onCancel}>Cancel</button></div>
  </form>;
}

function EditForm({ detail, hub, onSaved, onCancel, openTab }: { detail: ProductDetail; hub: Hub; onSaved: () => void; onCancel: () => void; openTab: OpenTab }) {
  const [title, setTitle] = useState(detail.title), [summary, setSummary] = useState(detail.summary);
  const [chosen, setChosen] = useState(detail.members.map((m) => m.asset_id)), [error, setError] = useState(""), [confirming, setConfirming] = useState(false);
  const [options, setOptions] = useState<OptionInput[]>((detail.options ?? []).map((o) => ({ name: o.name, description: o.description, default: o.default,
    masks: (o.masks ?? []).map((m) => ({ asset_id: m.asset_id, column: m.column, method: m.method, expression: m.expression })),
    filters: (o.filters ?? []).map((f) => ({ asset_id: f.asset_id, expression: f.expression })) })));
  const save = (e: React.FormEvent) => { e.preventDefault();
    void api.products.update(detail.id, { title: title.trim(), summary, asset_ids: chosen, options }).then(onSaved).catch((err: Error) => setError(err.message)); };
  return <form className="card" aria-label="Edit product" onSubmit={save}>
    <div className="arg-grid"><div className="field"><label htmlFor="product-title">Name in the catalog</label><input id="product-title" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} /></div>
      <div className="field" style={{ gridColumn: "1 / -1" }}><label htmlFor="product-summary">Summary</label><textarea id="product-summary" rows={2} maxLength={600} value={summary} onChange={(e) => setSummary(e.target.value)} /></div></div>
    <MemberPicker hub={hub} chosen={chosen} onChange={setChosen} />
    <p className="hint">Consumers get the use of what you add, and lose what you remove.</p>
    <OptionsEditor detail={detail} options={options} onChange={setOptions} />
    {error && <p className="error" role="alert">{error}</p>}
    <div className="form-actions"><button className="btn" disabled={!title.trim() || !chosen.length}>Save</button><button type="button" className="btn secondary" onClick={onCancel}>Cancel</button>
      <span className="grow" />
      {confirming ? <><span className="muted">Groups that got access through this product lose it.</span>
        <button type="button" className="btn danger" onClick={() => void api.products.unpublish(detail.id).then(() => openTab({ kind: "products", title: "Data products" })).catch((err: Error) => setError(err.message))}>Unpublish</button>
        <button type="button" className="btn secondary" onClick={() => setConfirming(false)}>Keep it</button></>
        : <button type="button" className="btn ghost danger" onClick={() => setConfirming(true)}>Unpublish…</button>}</div>
  </form>;
}

export function ProductView({ tab, hub, openTab }: { tab: TabRef; hub: Hub; openTab: OpenTab }) {
  const [detail, setDetail] = useState<ProductDetail | null>(null), [error, setError] = useState("");
  const [requesting, setRequesting] = useState(tab.intent === "request"), [editing, setEditing] = useState(false), [sent, setSent] = useState(false);
  const load = useCallback(() => api.products.get(tab.ref!).then((d) => { setDetail(d); setError(""); }).catch((e: Error) => setError(e.message)), [tab.ref]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (tab.intent === "request") setRequesting(true); }, [tab.intent]);
  if (error && !detail) return <p className="error" role="alert">{error}</p>;
  if (!detail) return <p className="muted" role="status">Loading…</p>;
  const section = tab.productSection ?? "overview";
  const target = openTarget(detail.open, detail.name, detail.kind);
  const sections: [NonNullable<TabRef["productSection"]>, string][] = [["overview", "Overview"], ["options", "Access options"], ["lineage", "Lineage"], ["used-by", "Used by"]];
  if (detail.access_resource) sections.push(["permissions", "Permissions"]);
  if (detail.access_resource && detail.can_manage) sections.push(["alerts", "Alerts"]);
  return <>
    <div className="crumbs muted"><button className="text-button" onClick={() => openTab({ kind: "products", title: "Data products" })}>Data products</button>{detail.domain && <> › {detail.domain}</>}</div>
    <div className="product-header">
      <BigIcon p={detail} size={34} />
      <div className="product-header-text"><h1>{detail.title}</h1>
        <p className="muted">{contents(detail.members)} · {detail.owner_group_name ?? "No owner group"}{detail.layer_name && ` · ${detail.layer_name}`}</p></div>
      <div className="row product-actions">
        {detail.status === "certified" && <span className="product-chip cert">Certified</span>}
        {(detail.access === "none" || detail.access === "partial") && !requesting && hub.has("products.request") && <button className="btn" onClick={() => setRequesting(true)}>Request access</button>}
        {detail.access !== "none" && <AccessState p={detail} />}
        {target && detail.kind === "app" && <OpenDashboard p={detail} hub={hub} openTab={openTab} />}
        {detail.can_manage && <button className="btn secondary" aria-expanded={editing} onClick={() => setEditing(!editing)}>Edit</button>}
      </div>
    </div>
    {sent && <p className="scope-note" role="status">Request sent. You will see the decision here and under Access requests → My requests.</p>}
    {requesting && hub.has("products.request") && <RequestForm p={detail} hub={hub} onCancel={() => setRequesting(false)} onDone={() => { setRequesting(false); setSent(true); void load(); }} />}
    {editing && <EditForm detail={detail} hub={hub} openTab={openTab} onCancel={() => setEditing(false)} onSaved={() => { setEditing(false); void load(); }} />}
    {error && <p className="error" role="alert">{error}</p>}
    <nav className="object-tabs" aria-label="Product sections">
      {sections.map(([id, label]) => <button key={id} className={section === id ? "active" : ""} aria-current={section === id ? "page" : undefined}
        onClick={() => openTab({ ...tab, intent: undefined, productSection: id })}>{label}</button>)}
    </nav>
    {section === "overview" && <>
      <section className="card"><p className="product-summary-lg">{detail.summary || <span className="muted">No summary yet.</span>}</p>
        <dl className="kv">
          <dt>Owner</dt><dd>{detail.owner_group_name ?? "—"}{detail.steward && ` · steward ${detail.steward}`}</dd>
          <dt>Domain</dt><dd>{detail.domain || "—"} · {words(detail.classification)}</dd>
          <dt>Certification</dt><dd>{words(detail.status)}</dd>
          <dt>Freshness</dt><dd>{detail.updated_at ? <>Updated {ago(detail.updated_at)}</> : "—"}{detail.sla_status && detail.sla_status !== "not_configured" && <> · SLA {words(detail.sla_status)}</>}</dd>
          <dt>Published</dt><dd>{ago(detail.published_at)} by {detail.published_by}</dd>
          <dt>Access</dt><dd>Consumers of this product get {grantsText(detail.grants)}.</dd>
        </dl></section>
      <div className="section-heading"><h2>In this product</h2></div>
      <ul className="product-uses" aria-label="In this product">{detail.members.map((m) => { const open = openTarget(m.open, m.name, m.kind); return <li key={m.asset_id}>
        <ProductIcon kind={m.kind} runtime={m.runtime ?? undefined} size={16} />
        {open ? <button className="text-button" onClick={() => openTab(open)}>{m.name}</button> : <span>{m.name}</span>}
        <span className="muted">{kindLabel(m.kind)}{m.updated_at ? ` · updated ${ago(m.updated_at)}` : ""}</span>
        {m.access === "granted" ? <span className="product-chip ok">You have access</span> : <span className="product-chip">No access</span>}</li>; })}</ul>
      {detail.access !== "granted" && <p className="hint">Columns, previews and SQL are shown on each object's own page once you have access.</p>}
    </>}
    {section === "options" && <AccessOptions detail={detail} onChanged={() => void load()} />}
    {section === "lineage" && <><Lineage detail={detail} openTab={openTab} />
      <p className="hint">Dashed steps are ones you cannot open: you see their name, kind and owner. {hub.has("catalog.objects") && detail.members.some((m) => m.access === "granted") &&
        <button className="text-button" onClick={() => { const m = detail.members.find((x) => x.access === "granted")!; openTab({ kind: "catalog-object", ref: m.asset_id, title: m.name, catalogSection: "lineage" }); }}>Open the full lineage graph</button>}</p></>}
    {section === "used-by" && <>{detail.used_by.length ? <div className="product-grid">{detail.used_by.map((p) => <ProductCard key={p.id} p={p} openTab={openTab} hub={hub} />)}</div>
      : <p className="muted">No other published products are built on this one.</p>}
      {detail.used_by_others > 0 && <p className="hint">{detail.used_by_others} other object{detail.used_by_others === 1 ? " uses" : "s use"} it (not published).</p>}</>}
    {section === "alerts" && detail.access_resource && <AlertRules hub={hub} resource={detail.access_resource} />}
    {section === "permissions" && detail.access_resource && <PermissionsPanel resource={detail.access_resource} resources={[detail.access_resource]} openTab={openTab} onChanged={async () => { await load(); }} />}
  </>;
}

/** The product's access options: what each shows, the level that leads to, and (for owners) which groups have it. */
function AccessOptions({ detail, onChanged }: { detail: ProductDetail; onChanged: () => void }) {
  const [groups, setGroups] = useState<AppGroup[]>([]), [group, setGroup] = useState(""), [option, setOption] = useState(""), [error, setError] = useState("");
  useEffect(() => { if (detail.can_manage) api.access.groups().then(setGroups).catch(() => setGroups([])); }, [detail.can_manage]);
  const consumers = detail.consumers ?? [];
  const [justify, setJustify] = useState<{ target: CaseTarget; requirement: Requirement } | null>(null), [message, setMessage] = useState("");
  // Consumers from before the product's data needed a form: they stay until their deadline.
  const [deadlines, setDeadlines] = useState<JustifyDeadline[]>([]);
  useEffect(() => { if (detail.can_manage) api.compliance.deadlines().then((all) => setDeadlines(all.filter((d) => d.product_id === detail.id))).catch(() => setDeadlines([])); }, [detail.id, detail.can_manage]);
  const justifyExisting = (groupId: string, optionId: string) => { setError(""); setMessage("");
    const target: CaseTarget = { kind: "product", app_group_id: groupId, product_id: detail.id, option_id: optionId };
    api.compliance.requirement(target).then((requirement) => setJustify({ target, requirement })).catch((e: Error) => setError(e.message)); };
  const act = (p: Promise<unknown>, target?: CaseTarget) => { setError(""); setMessage(""); setJustify(null); void p.then(onChanged).catch((e: Error) => {
    // Data under an access policy: the group becomes a consumer once a compliance form is approved.
    if (target && e instanceof ApiError && e.requirement) setJustify({ target, requirement: e.requirement });
    setError(e.message);
  }); };
  return <>
    <p className="hint">Each option is a way to use this product: the masks and row filters its consumers get. The level shows what consumers of the option really see; masked columns count lower.</p>
    <div className="table-scroll"><table aria-label="Access options"><thead><tr><th>Option</th><th>Consumers see</th><th>Level</th>{detail.can_manage && <th>Groups</th>}</tr></thead><tbody>
      {(detail.options ?? []).map((o) => <tr key={o.id}><td><strong>{o.name}</strong>{o.default && <span className="muted"> · default</span>}{o.description && <div className="muted small">{o.description}</div>}</td>
        <td>{o.masks ? <>{o.masks.map((m) => <div key={m.table + m.column} className="mono small">{m.table}.{m.column}: {m.method}</div>)}
            {(o.filters ?? []).map((f) => <div key={f.table} className="mono small">{f.table}: rows where {f.expression}</div>)}{!o.masks.length && !(o.filters ?? []).length && <span className="muted">everything, unmasked</span>}</>
          : <span className="muted">{o.masked_columns ? `${o.masked_columns} masked column${o.masked_columns === 1 ? "" : "s"}` : "no masked columns"}{o.filtered_tables ? ` · rows filtered on ${o.filtered_tables} table${o.filtered_tables === 1 ? "" : "s"}` : ""}</span>}</td>
        <td><span className="product-chip">{words(o.classification)}</span></td>
        {detail.can_manage && <td>{consumers.filter((c) => c.option_id === o.id).map((c) => <div key={c.group_id} className="row" style={{ gap: 6 }}>{c.group}
          <select aria-label={`Option of ${c.group}`} value={o.id} onChange={(e) => act(api.products.setConsumer(detail.id, c.group_id, e.target.value))}>
            {(detail.options ?? []).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select>
          <button className="text-button" onClick={() => act(api.products.removeConsumer(detail.id, c.group_id))}>Remove</button>
          {(() => { const d = deadlines.find((x) => x.app_group_id === c.group_id); return d && <span className="small">
            <span className="error-text">Needs a form by {new Date(d.due_at).toLocaleDateString()}</span>{" "}
            <button className="text-button" onClick={() => justifyExisting(c.group_id, o.id)}>Justify</button></span>; })()}</div>)}
          {!consumers.some((c) => c.option_id === o.id) && <span className="muted">—</span>}</td>}
      </tr>)}</tbody></table></div>
    {detail.can_manage && <form className="row" style={{ gap: 8, marginTop: 12, flexWrap: "wrap" }} onSubmit={(e) => { e.preventDefault(); act(api.products.setConsumer(detail.id, group, option || undefined),
      { kind: "product", app_group_id: group, product_id: detail.id, option_id: option || undefined }); }}>
      <select aria-label="Group to give access" value={group} onChange={(e) => setGroup(e.target.value)}><option value="">Give a group access…</option>
        {groups.filter((g) => !consumers.some((c) => c.group_id === g.id)).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select>
      <select aria-label="With option" value={option} onChange={(e) => setOption(e.target.value)}><option value="">Default option</option>{(detail.options ?? []).map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</select>
      <button className="btn secondary" disabled={!group}>Give access</button></form>}
    {error && !justify && <p className="error" role="alert">{error}</p>}
    {message && <p className="success-message" role="status">{message}</p>}
    {justify && <JustifyPanel target={justify.target} requirement={justify.requirement} onCancel={() => { setJustify(null); setError(""); }}
      onDone={(text) => { setJustify(null); setError(""); setMessage(text); onChanged(); }} />}
  </>;
}

/** Owners shape the options: per option masks on table columns and a row filter per table. */
function OptionsEditor({ detail, options, onChange }: { detail: ProductDetail; options: OptionInput[]; onChange: (o: OptionInput[]) => void }) {
  const tables = detail.members.filter((m) => ["table", "view", "materialized_view"].includes(m.kind));
  const set = (i: number, patch: Partial<OptionInput>) => onChange(options.map((o, j) => j === i ? { ...o, ...patch } : patch.default ? { ...o, default: false } : o));
  return <fieldset className="publish-members options-editor"><legend>Access options</legend>
    <p className="hint">The default option is what a request gets unless another is chosen. Masks and filters apply to people who reach these tables only through this product.</p>
    {options.map((o, i) => <div key={i} className="option-editor" aria-label={`Option ${o.name || i + 1}`} role="group">
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <input aria-label="Option name" value={o.name} onChange={(e) => set(i, { name: e.target.value })} placeholder="Masked" />
        <input aria-label="Option description" value={o.description} onChange={(e) => set(i, { description: e.target.value })} placeholder="What consumers see" style={{ flex: 1, minWidth: 200 }} />
        <label className="row"><input type="radio" name="default-option" checked={o.default} onChange={() => set(i, { default: true })} />Default</label>
        <button type="button" className="text-button" disabled={options.length === 1} onClick={() => onChange(options.filter((_, j) => j !== i))}>Remove option</button></div>
      {o.masks.map((m, k) => <div key={k} className="row" style={{ gap: 6, marginTop: 4 }}>
        <select aria-label="Masked table" value={m.asset_id} onChange={(e) => set(i, { masks: o.masks.map((x, y) => y === k ? { ...x, asset_id: e.target.value } : x) })}>
          {tables.map((t) => <option key={t.asset_id} value={t.asset_id}>{t.name}</option>)}</select>
        <input aria-label="Masked column" value={m.column} placeholder="email" onChange={(e) => set(i, { masks: o.masks.map((x, y) => y === k ? { ...x, column: e.target.value } : x) })} />
        <select aria-label="Mask method" value={m.method} onChange={(e) => set(i, { masks: o.masks.map((x, y) => y === k ? { ...x, method: e.target.value } : x) })}>
          {["hash", "redact", "partial", "null", "expression"].map((v) => <option key={v}>{v}</option>)}</select>
        {m.method === "expression" && <input aria-label="Mask expression" value={m.expression} onChange={(e) => set(i, { masks: o.masks.map((x, y) => y === k ? { ...x, expression: e.target.value } : x) })} />}
        <button type="button" className="text-button" onClick={() => set(i, { masks: o.masks.filter((_, y) => y !== k) })}>Remove</button></div>)}
      {o.filters.map((f, k) => <div key={"f" + k} className="row" style={{ gap: 6, marginTop: 4 }}>
        <select aria-label="Filtered table" value={f.asset_id} onChange={(e) => set(i, { filters: o.filters.map((x, y) => y === k ? { ...x, asset_id: e.target.value } : x) })}>
          {tables.map((t) => <option key={t.asset_id} value={t.asset_id}>{t.name}</option>)}</select>
        <input aria-label="Row filter" value={f.expression} placeholder="region = 'EU'" style={{ flex: 1 }} onChange={(e) => set(i, { filters: o.filters.map((x, y) => y === k ? { ...x, expression: e.target.value } : x) })} />
        <button type="button" className="text-button" onClick={() => set(i, { filters: o.filters.filter((_, y) => y !== k) })}>Remove</button></div>)}
      {!!tables.length && <div className="row" style={{ gap: 8, marginTop: 4 }}>
        <button type="button" className="text-button" onClick={() => set(i, { masks: [...o.masks, { asset_id: tables[0].asset_id, column: "", method: "hash", expression: "" }] })}>+ Mask a column</button>
        <button type="button" className="text-button" onClick={() => set(i, { filters: [...o.filters, { asset_id: tables[0].asset_id, expression: "" }] })}>+ Filter rows</button></div>}
    </div>)}
    <button type="button" className="text-button" onClick={() => onChange([...options, { name: "", description: "", default: false, masks: [], filters: [] }])}>+ Add option</button>
  </fieldset>;
}

/** Owners approve or decline requests; everyone sees their own. */
export function AccessRequestsView({ hub, openTab, tab }: { hub: Hub; openTab: OpenTab; tab: TabRef }) {
  const box = tab.ref === "mine" ? "mine" : "approve";
  const [rows, setRows] = useState<AccessRequestItem[] | null>(null), [error, setError] = useState(""), [notes, setNotes] = useState<Record<string, string>>({});
  const load = useCallback(() => api.products.requests(box).then((r) => { setRows(r); setError(""); }).catch((e: Error) => setError(e.message)), [box]);
  useEffect(() => { void load(); }, [load]);
  // Compliance forms: role grants waiting for another owner, or everything you sent.
  const [cases, setCases] = useState<AccessCaseItem[]>([]);
  useEffect(() => { api.compliance.cases(box === "mine" ? "mine" : "review").then((list) => setCases(box === "mine" ? list : list.filter((c) => c.kind === "role")))
    .catch(() => setCases([])); }, [box]);
  const decide = (r: AccessRequestItem, approve: boolean) => api.products.decide(r.id, approve, notes[r.id] ?? "").then(() => { void load(); void hub.refresh(); }).catch((e: Error) => setError(e.message));
  return <>
    <PageHeader icon={<IconCatalog size={22} />} title="Access requests" subtitle={box === "approve" ? "Requests for the data products your groups own. Approving makes the requested group a Consumer of the product." : "Your requests and their decisions."} />
    <nav className="object-tabs" aria-label="Request boxes">
      <button className={box === "approve" ? "active" : ""} aria-current={box === "approve" ? "page" : undefined} onClick={() => openTab({ kind: "access-requests", title: "Access requests" })}>To approve</button>
      <button className={box === "mine" ? "active" : ""} aria-current={box === "mine" ? "page" : undefined} onClick={() => openTab({ kind: "access-requests", ref: "mine", title: "My requests" })}>My requests</button>
    </nav>
    {error && <p className="error" role="alert">{error}</p>}
    {!rows ? <p className="muted" role="status">Loading…</p> : !rows.length ? <p className="muted">{box === "approve" ? "No requests for your products." : "You have not requested access to anything."}</p>
      : <div className="table-scroll"><table aria-label={box === "approve" ? "Requests to approve" : "My requests"}><thead><tr><th>Product</th><th>{box === "approve" ? "Requested by" : "Requested"}</th><th>For group · why</th><th>Status</th><th /></tr></thead><tbody>
        {rows.map((r) => <tr key={r.id}>
          <td><button className="text-button" onClick={() => openTab({ kind: "product", ref: r.product_id, title: r.product })}>{r.product}</button>
            <div className="muted small">gives {grantsText(r.grants)}</div></td>
          <td>{box === "approve" ? r.requester : ""} <span className="muted" title={new Date(r.created_at).toLocaleString()}>{ago(r.created_at)}</span></td>
          <td>{r.group}{r.option && <span className="muted"> · {r.option}</span>}<div className="muted">{r.reason}</div></td>
          <td><span className="status"><span className={`dot ${r.status === "approved" ? "succeeded" : r.status === "pending" ? "queued" : r.status === "declined" ? "failed" : "stopped"}`} />{r.status}</span>
            {r.decided_by && <div className="muted small">{r.decided_by}{r.decision_note && `: ${r.decision_note}`}</div>}
            {r.case_id && <div className="small"><button className="text-button" onClick={() => openTab({ kind: "case", ref: r.case_id!, title: r.product })}>
              Compliance form{r.case_status ? ` · ${r.case_status.replace("_", " ")}` : ""}</button></div>}</td>
          <td className="table-actions">{r.status === "pending" && (box === "approve" ? <div className="row">
            <input aria-label={`Note for ${r.requester}`} placeholder="Note (optional)" value={notes[r.id] ?? ""} onChange={(e) => setNotes({ ...notes, [r.id]: e.target.value })} />
            <button className="btn sm" onClick={() => void decide(r, true)}>Approve</button><button className="btn sm secondary" onClick={() => void decide(r, false)}>Decline</button></div>
            : <button className="btn sm secondary" onClick={() => void api.products.withdraw(r.id).then(load)}>Withdraw</button>)}</td>
        </tr>)}</tbody></table></div>}
    {!!cases.length && <><h2>{box === "mine" ? "Your compliance forms" : "Compliance forms for your data"}</h2>
      <CaseTable cases={cases} openTab={openTab} tabKind="case" requester={box !== "mine"} label={box === "mine" ? "Your compliance forms" : "Compliance forms to review"} /></>}
  </>;
}

/** On an object's own page: publish it (with what it uses), or open the product it is in. */
export function PublishButton({ resourceId, objectPath, openTab, hub }: { resourceId?: string | null; objectPath?: string; openTab: OpenTab; hub?: Hub }) {
  const [state, setState] = useState<{ assetId: string; info: AssetProduct } | null>(null);
  const allowed = !hub || hub.has("products.publish");
  useEffect(() => {
    if (!resourceId || !allowed) return;
    let alive = true;
    api.catalog.resourceAsset(resourceId, objectPath).then((asset) => api.products.forAsset(asset.id).then((info) => alive && setState({ assetId: asset.id, info })))
      .catch(() => alive && setState(null));
    return () => { alive = false; };
  }, [resourceId, objectPath, allowed]);
  if (!allowed || !state || !state.info.publishable) return null;
  const product = state.info.products[0];
  if (product) return <button className="btn secondary" title={state.info.products.map((p) => p.title).join(", ")} onClick={() => openTab(productTab(product))}>
    In {state.info.products.length === 1 ? product.title : `${state.info.products.length} products`}</button>;
  if (!state.info.can_publish) return null;
  return <button className="btn secondary" onClick={() => openTab({ kind: "products", ref: "mine", title: "My products", intent: "publish", assetId: state.assetId })}>Publish to catalog</button>;
}
