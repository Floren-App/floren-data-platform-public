import { useCallback, useEffect, useState } from "react";
import { AccessRequestItem, ago, api, AssetProduct, Product, ProductCandidate, ProductDetail, ProductStep } from "../api";
import type { Hub } from "../hub";
import { PageHeader } from "../components/Page";
import { ProductIcon } from "../components/ProductIcon";
import { IconCatalog, IconPlus } from "../components/Icons";
import { objectKindLabel } from "../objectKinds";
import type { TabRef } from "./Tabs";

type OpenTab = (tab: TabRef) => void;

export const productTab = (p: Pick<Product, "id" | "title">, extra: Partial<TabRef> = {}): TabRef => ({ kind: "product", ref: p.id, title: p.title, ...extra });
const KIND: Record<string, string> = { app: "Dashboard", table: "Table", view: "View", materialized_view: "Materialized view", api: "API", topic: "Kafka topic" };
const kindLabel = (kind: string) => KIND[kind] ?? objectKindLabel(kind);
const words = (value: string) => value.replaceAll("_", " ");

/** Where to open the object itself: the dashboard, the table, the API or the topic (only with access). */
export function openTarget(p: Product): TabRef | null {
  const o = p.open;
  if (!o) return null;
  if (o.resource_kind === "service" && o.object_id) return { kind: "workload", ref: o.object_id, title: p.title };
  if (o.resource_kind === "schema" && o.object_path) return { kind: "data-object", ref: JSON.stringify([o.resource_id, p.kind, o.object_path]), title: o.object_path,
    data: { schemaId: o.resource_id, table: o.object_path, item: { name: o.object_path, kind: p.kind as "table" } } };
  if (o.resource_kind === "api" && o.object_id) return { kind: "api", ref: o.object_id, title: p.title };
  if (o.resource_kind === "topic") return { kind: "kafka", kafkaKind: "topic", ref: o.resource_id, title: p.title };
  return null;
}

function BigIcon({ p, size = 30 }: { p: Pick<Product, "kind" | "runtime">; size?: number }) {
  return <span className={`product-mark kind-${p.kind}`} aria-hidden="true"><ProductIcon kind={p.kind === "topic" ? "topic" : p.kind} runtime={p.runtime ?? undefined} size={size} /></span>;
}

function AccessState({ p }: { p: Product }) {
  return p.access === "granted" ? <span className="product-chip ok">You have access</span>
    : p.access === "requested" ? <span className="product-chip wait">Requested</span>
    : <span className="product-chip">No access yet</span>;
}

export function ProductCard({ p, openTab }: { p: Product; openTab: OpenTab }) {
  return <article className="product-card" aria-label={p.title}>
    <BigIcon p={p} />
    <div className="product-card-head">
      <button className="text-button product-title" onClick={() => openTab(productTab(p))}>{p.title}</button>
      <span className="muted">{p.owner_group_name ?? "No owner group"}{p.domain && ` · ${p.domain}`}</span>
    </div>
    <p className="product-summary">{p.summary || <span className="muted">No summary yet.</span>}</p>
    <div className="product-card-foot">
      <span className="product-chip">{kindLabel(p.kind)}</span>
      {p.status === "certified" && <span className="product-chip cert">Certified</span>}
      {p.updated_at && <span className="product-chip" title={new Date(p.updated_at).toLocaleString()}>Updated {ago(p.updated_at)}</span>}
      <span className="grow" />
      {p.access === "none" ? <button className="btn sm" onClick={() => openTab(productTab(p, { intent: "request" }))}>Request access</button> : <AccessState p={p} />}
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
      subtitle={mine ? "Products you publish and manage. Change their summary, unpublish them, and answer access requests."
        : `Data products shared in ${layer ?? "this layer"}. Open one to see where it comes from and what uses it; request access to the ones you need.`}
      actions={<button className="btn" aria-expanded={publishing} onClick={() => setPublishing(!publishing)}><IconPlus size={14} />Publish a product</button>} />
    {publishing && <PublishPanel hub={hub} assetId={tab.assetId} onClose={() => setPublishing(false)}
      onPublished={(p) => { setPublishing(false); setVersion((v) => v + 1); openTab(productTab(p)); }} />}
    <div className="object-toolbar product-filters">
      <input type="search" aria-label="Search data products" placeholder="Search products, owners, domains…" value={q} onChange={(e) => setQ(e.target.value)} />
      <select aria-label="Domain" value={domain} onChange={(e) => setDomain(e.target.value)}><option value="">All domains</option>{data?.domains.map((d) => <option key={d}>{d}</option>)}</select>
      <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value)}><option value="">All kinds</option>{data?.kinds.map((k) => <option key={k} value={k}>{kindLabel(k)}</option>)}</select>
      {!mine && <label className="row muted"><input type="checkbox" checked={accessible} onChange={(e) => setAccessible(e.target.checked)} />Only what I can use</label>}
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {!data ? <p className="muted" role="status">Loading data products…</p>
      : !data.items.length ? <div className="empty-state"><p>{q || domain || kind || accessible ? "No products match." : mine ? "You have not published anything in this layer yet."
          : "Nothing is published in this layer yet."}</p><p className="muted">Owners publish tables, dashboards, APIs and Kafka topics from their pages, or with Publish a product.</p></div>
      : <div className="product-grid">{data.items.map((p) => <ProductCard key={p.id} p={p} openTab={openTab} />)}</div>}
  </>;
}

/** Publish: pick an object you own (or the one given), say what it is, and optionally publish what it uses. */
export function PublishPanel({ hub, assetId, onPublished, onClose }: { hub: Hub; assetId?: string; onPublished: (p: Product) => void; onClose: () => void }) {
  const [candidates, setCandidates] = useState<ProductCandidate[] | null>(null);
  const [chosen, setChosen] = useState(assetId ?? ""), [info, setInfo] = useState<AssetProduct | null>(null);
  const [title, setTitle] = useState(""), [summary, setSummary] = useState(""), [domain, setDomain] = useState(""), [classification, setClassification] = useState("internal");
  const [also, setAlso] = useState<string[]>([]), [busy, setBusy] = useState(false), [error, setError] = useState("");
  useEffect(() => { api.products.candidates(hub.layerId).then(setCandidates).catch((e: Error) => setError(e.message)); }, [hub.layerId]);
  useEffect(() => { if (assetId) setChosen(assetId); }, [assetId]);
  useEffect(() => {
    setInfo(null); setAlso([]);
    if (!chosen) return;
    api.products.forAsset(chosen).then((i) => { setInfo(i); setDomain(i.domain); if (i.classification !== "unclassified") setClassification(i.classification);
      setAlso(i.uses.filter((u) => !u.published).map((u) => u.asset_id)); }).catch((e: Error) => setError(e.message));
    const c = candidates?.find((x) => x.asset_id === chosen);
    if (c) setTitle((t) => t || c.name);
  }, [chosen, candidates]);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try { onPublished(await api.products.publish({ asset_id: chosen, title, summary, domain, classification, also })); }
    catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  return <form className="card publish-panel" aria-label="Publish a data product" onSubmit={(e) => void submit(e)}>
    <h2>Publish a data product</h2>
    <p className="muted">Everyone in this layer will see its card: name, summary, owner, domain and freshness. Details stay behind its permissions; people ask you for access.</p>
    <div className="arg-grid">
      <div className="field"><label htmlFor="publish-object">Object</label>
        <select id="publish-object" value={chosen} onChange={(e) => { setChosen(e.target.value); setTitle(""); }}>
          <option value="">{candidates ? candidates.length ? "Choose a table, dashboard, API or topic you own" : "Nothing you own to publish here" : "Loading…"}</option>
          {chosen && !candidates?.some((c) => c.asset_id === chosen) && <option value={chosen}>{info?.product?.name ?? "This object"}</option>}
          {candidates?.map((c) => <option key={c.asset_id} value={c.asset_id}>{kindLabel(c.kind)} · {c.name}{c.path ? ` (${c.path})` : ""}</option>)}
        </select></div>
      <div className="field"><label htmlFor="publish-title">Name in the catalog</label><input id="publish-title" maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Sales dashboard" /></div>
      <div className="field" style={{ gridColumn: "1 / -1" }}><label htmlFor="publish-summary">Summary</label>
        <textarea id="publish-summary" rows={2} maxLength={600} value={summary} onChange={(e) => setSummary(e.target.value)} placeholder="What it shows or contains, for whom, and how often it updates." /></div>
      <div className="field"><label htmlFor="publish-domain">Domain</label><input id="publish-domain" maxLength={80} value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="Finance" /></div>
      <div className="field"><label htmlFor="publish-classification">Classification</label>
        <select id="publish-classification" value={classification} onChange={(e) => setClassification(e.target.value)}>
          {["public", "internal", "confidential", "restricted"].map((c) => <option key={c}>{c}</option>)}</select></div>
    </div>
    {info?.product && <p className="hint">Already published as <strong>{info.product.title}</strong>.</p>}
    {!!info?.uses.length && <fieldset className="publish-also"><legend>Also publish what it uses</legend>
      {info.uses.map((u) => <label key={u.asset_id} className="row"><input type="checkbox" disabled={u.published} checked={u.published || also.includes(u.asset_id)}
        onChange={(e) => setAlso(e.target.checked ? [...also, u.asset_id] : also.filter((x) => x !== u.asset_id))} />{kindLabel(u.kind)} · {u.name}{u.published && <span className="muted"> · already published</span>}</label>)}
    </fieldset>}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="form-actions"><button className="btn" disabled={busy || !chosen || !!info?.product || (info !== null && !info.can_publish)}>{busy ? "Publishing…" : "Publish"}</button>
      <button type="button" className="btn secondary" onClick={onClose}>Cancel</button></div>
  </form>;
}

/** The lineage of a product, from its sources (left) to the product (right). Steps you cannot open are dashed. */
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
        <small className="muted">{kindLabel(s.kind)}{s.owner_group_name ? ` · ${s.owner_group_name}` : ""}{s.product_id ? " · product" : ""}</small>
      </div>)}
    </div>)}
    <div className="product-lineage-column"><div role="listitem" className="product-step current"><span className="row"><ProductIcon kind={detail.kind} runtime={detail.runtime ?? undefined} size={16} /><strong>{detail.title}</strong></span><small className="muted">This product</small></div></div>
  </div></div>;
}

function RequestForm({ p, hub, onDone, onCancel }: { p: Product; hub: Hub; onDone: () => void; onCancel: () => void }) {
  const groups = hub.me?.available_groups ?? [];
  const [group, setGroup] = useState(hub.me?.active_group_id && groups.some((g) => g.id === hub.me?.active_group_id) ? hub.me.active_group_id : groups[0]?.id ?? "");
  const [reason, setReason] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try { await api.products.requestAccess(p.id, group, reason.trim()); onDone(); } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  return <form className="card" aria-label={`Request access to ${p.title}`} onSubmit={(e) => void submit(e)}>
    <h2>Request access</h2>
    <p className="muted">{p.owner_group_name ?? "The owners"} decide. Approval gives the group you choose {p.grants} on {p.grants_on.kind === "schema"
      ? <>schema <strong>{p.grants_on.name}</strong>, which holds this table</> : <strong>{p.title}</strong>}.</p>
    <div className="arg-grid">
      <div className="field"><label htmlFor="request-group">For your group</label>
        <select id="request-group" value={group} onChange={(e) => setGroup(e.target.value)}>{groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select></div>
      <div className="field" style={{ gridColumn: "1 / -1" }}><label htmlFor="request-reason">Why you need it</label>
        <textarea id="request-reason" rows={2} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="What you will use it for" /></div>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="form-actions"><button className="btn" disabled={busy || !group || reason.trim().length < 3}>{busy ? "Sending…" : "Send request"}</button>
      <button type="button" className="btn secondary" onClick={onCancel}>Cancel</button></div>
  </form>;
}

export function ProductView({ tab, hub, openTab }: { tab: TabRef; hub: Hub; openTab: OpenTab }) {
  const [detail, setDetail] = useState<ProductDetail | null>(null), [error, setError] = useState("");
  const [requesting, setRequesting] = useState(tab.intent === "request"), [editing, setEditing] = useState(false), [sent, setSent] = useState(false);
  const [title, setTitle] = useState(""), [summary, setSummary] = useState("");
  const load = useCallback(() => api.products.get(tab.ref!).then((d) => { setDetail(d); setError(""); }).catch((e: Error) => setError(e.message)), [tab.ref]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (tab.intent === "request") setRequesting(true); }, [tab.intent]);
  if (error && !detail) return <p className="error" role="alert">{error}</p>;
  if (!detail) return <p className="muted" role="status">Loading…</p>;
  const section = tab.productSection ?? "overview";
  const target = openTarget(detail);
  const uses = detail.lineage.filter((s) => s.depth === 1);
  return <>
    <div className="crumbs muted"><button className="text-button" onClick={() => openTab({ kind: "products", title: "Data products" })}>Data products</button>{detail.domain && <> › {detail.domain}</>}</div>
    <div className="product-header">
      <BigIcon p={detail} size={34} />
      <div className="product-header-text"><h1>{detail.title}</h1>
        <p className="muted">{kindLabel(detail.kind)} · {detail.owner_group_name ?? "No owner group"}{detail.layer_name && ` · ${detail.layer_name}`}</p></div>
      <div className="row product-actions">
        {detail.status === "certified" && <span className="product-chip cert">Certified</span>}
        {detail.access === "none" && !requesting && <button className="btn" onClick={() => setRequesting(true)}>Request access</button>}
        {detail.access !== "none" && <AccessState p={detail} />}
        {target && <button className="btn secondary" onClick={() => openTab(target)}>{detail.kind === "app" ? "Open dashboard" : "Open"}</button>}
        {detail.can_manage && <button className="btn secondary" onClick={() => { setTitle(detail.title); setSummary(detail.summary); setEditing(!editing); }}>Edit</button>}
      </div>
    </div>
    {sent && <p className="scope-note" role="status">Request sent. You will see the decision here and under Access requests → My requests.</p>}
    {requesting && <RequestForm p={detail} hub={hub} onCancel={() => setRequesting(false)} onDone={() => { setRequesting(false); setSent(true); void load(); }} />}
    {editing && <form className="card" aria-label="Edit product" onSubmit={(e) => { e.preventDefault(); void api.products.update(detail.id, { title, summary }).then(() => { setEditing(false); return load(); }).catch((err: Error) => setError(err.message)); }}>
      <div className="arg-grid"><div className="field"><label htmlFor="product-title">Name in the catalog</label><input id="product-title" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} /></div>
        <div className="field" style={{ gridColumn: "1 / -1" }}><label htmlFor="product-summary">Summary</label><textarea id="product-summary" rows={2} maxLength={600} value={summary} onChange={(e) => setSummary(e.target.value)} /></div></div>
      <div className="form-actions"><button className="btn" disabled={!title.trim()}>Save</button><button type="button" className="btn secondary" onClick={() => setEditing(false)}>Cancel</button>
        <span className="grow" /><button type="button" className="btn ghost danger" onClick={() => void api.products.unpublish(detail.id).then(() => openTab({ kind: "products", title: "Data products" })).catch((err: Error) => setError(err.message))}>Unpublish</button></div>
    </form>}
    {error && <p className="error" role="alert">{error}</p>}
    <nav className="object-tabs" aria-label="Product sections">
      {([["overview", "Overview"], ["lineage", "Lineage"], ["used-by", "Used by"]] as const).map(([id, label]) =>
        <button key={id} className={section === id ? "active" : ""} aria-current={section === id ? "page" : undefined} onClick={() => openTab({ ...tab, intent: undefined, productSection: id })}>{label}</button>)}
    </nav>
    {section === "overview" && <>
      <section className="card"><p className="product-summary-lg">{detail.summary || <span className="muted">No summary yet.</span>}</p>
        <dl className="kv">
          <dt>Owner</dt><dd>{detail.owner_group_name ?? "—"}{detail.steward && ` · steward ${detail.steward}`}</dd>
          <dt>Domain</dt><dd>{detail.domain || "—"} · {words(detail.classification)}</dd>
          <dt>Certification</dt><dd>{words(detail.status)}</dd>
          <dt>Freshness</dt><dd>{detail.updated_at ? <>Updated {ago(detail.updated_at)}</> : "—"}{detail.sla_status && detail.sla_status !== "not_configured" && <> · SLA {words(detail.sla_status)}</>}</dd>
          <dt>Published</dt><dd>{ago(detail.published_at)} by {detail.published_by}</dd>
          <dt>Access</dt><dd>{detail.grants} on {detail.grants_on.kind === "schema" ? <>schema <span className="mono">{detail.grants_on.name}</span> (all its tables)</> : detail.title}</dd>
        </dl>
        {detail.access !== "granted" && <p className="hint">Columns, previews and SQL are shown once you have access.</p>}</section>
      {uses.length > 0 && <><div className="section-heading"><h2>{detail.kind === "app" ? "Data it uses" : "Made from"}</h2></div>
        <ul className="product-uses">{uses.map((s) => <li key={s.key}><ProductIcon kind={s.kind} size={16} />
          {s.product_id ? <button className="text-button" onClick={() => openTab({ kind: "product", ref: s.product_id!, title: s.name })}>{s.name}</button> : <span>{s.name}</span>}
          <span className="muted">{kindLabel(s.kind)}{s.owner_group_name ? ` · ${s.owner_group_name}` : ""}</span>
          {s.product_id ? <span className="product-chip">Data product</span> : !s.visible && <span className="muted">not shared</span>}</li>)}</ul></>}
    </>}
    {section === "lineage" && <><Lineage detail={detail} openTab={openTab} />
      <p className="hint">Dashed steps are ones you cannot open: you see their name, kind and owner. {detail.access === "granted" && <button className="text-button" onClick={() => openTab({ kind: "catalog-object", ref: detail.asset_id, title: detail.name, catalogSection: "lineage" })}>Open the full lineage graph</button>}</p></>}
    {section === "used-by" && <>{detail.used_by.length ? <div className="product-grid">{detail.used_by.map((p) => <ProductCard key={p.id} p={p} openTab={openTab} />)}</div>
      : <p className="muted">No published products are built on this one.</p>}
      {detail.used_by_others > 0 && <p className="hint">{detail.used_by_others} other object{detail.used_by_others === 1 ? " uses" : "s use"} it (not published).</p>}</>}
  </>;
}

/** Owners approve or decline requests; everyone sees their own. */
export function AccessRequestsView({ hub, openTab, tab }: { hub: Hub; openTab: OpenTab; tab: TabRef }) {
  const box = tab.ref === "mine" ? "mine" : "approve";
  const [rows, setRows] = useState<AccessRequestItem[] | null>(null), [error, setError] = useState(""), [notes, setNotes] = useState<Record<string, string>>({});
  const load = useCallback(() => api.products.requests(box).then((r) => { setRows(r); setError(""); }).catch((e: Error) => setError(e.message)), [box]);
  useEffect(() => { void load(); }, [load]);
  const decide = (r: AccessRequestItem, approve: boolean) => api.products.decide(r.id, approve, notes[r.id] ?? "").then(() => { void load(); void hub.refresh(); }).catch((e: Error) => setError(e.message));
  return <>
    <PageHeader icon={<IconCatalog size={22} />} title="Access requests" subtitle={box === "approve" ? "Requests for the data products you own. Approving gives the requested group the product's reader role." : "Your requests and their decisions."} />
    <nav className="object-tabs" aria-label="Request boxes">
      <button className={box === "approve" ? "active" : ""} aria-current={box === "approve" ? "page" : undefined} onClick={() => openTab({ kind: "access-requests", title: "Access requests" })}>To approve</button>
      <button className={box === "mine" ? "active" : ""} aria-current={box === "mine" ? "page" : undefined} onClick={() => openTab({ kind: "access-requests", ref: "mine", title: "My requests" })}>My requests</button>
    </nav>
    {error && <p className="error" role="alert">{error}</p>}
    {!rows ? <p className="muted" role="status">Loading…</p> : !rows.length ? <p className="muted">{box === "approve" ? "No requests for your products." : "You have not requested access to anything."}</p>
      : <div className="table-scroll"><table aria-label={box === "approve" ? "Requests to approve" : "My requests"}><thead><tr><th>Product</th><th>{box === "approve" ? "Requested by" : "Requested"}</th><th>For group · why</th><th>Status</th><th /></tr></thead><tbody>
        {rows.map((r) => <tr key={r.id}>
          <td><button className="text-button" onClick={() => openTab({ kind: "product", ref: r.product_id, title: r.product })}>{r.product}</button>
            {r.grants_on?.kind === "schema" && <div className="muted small">grants {r.role} on schema {r.grants_on.name}</div>}</td>
          <td>{box === "approve" ? r.requester : ""} <span className="muted" title={new Date(r.created_at).toLocaleString()}>{ago(r.created_at)}</span></td>
          <td>{r.group}<div className="muted">{r.reason}</div></td>
          <td><span className="status"><span className={`dot ${r.status === "approved" ? "succeeded" : r.status === "pending" ? "queued" : r.status === "declined" ? "failed" : "stopped"}`} />{r.status}</span>
            {r.decided_by && <div className="muted small">{r.decided_by}{r.decision_note && `: ${r.decision_note}`}</div>}</td>
          <td className="table-actions">{r.status === "pending" && (box === "approve" ? <div className="row">
            <input aria-label={`Note for ${r.requester}`} placeholder="Note (optional)" value={notes[r.id] ?? ""} onChange={(e) => setNotes({ ...notes, [r.id]: e.target.value })} />
            <button className="btn sm" onClick={() => void decide(r, true)}>Approve</button><button className="btn sm secondary" onClick={() => void decide(r, false)}>Decline</button></div>
            : <button className="btn sm secondary" onClick={() => void api.products.withdraw(r.id).then(load)}>Withdraw</button>)}</td>
        </tr>)}</tbody></table></div>}
  </>;
}

/** On an object's own page: publish it, or see that it is published. */
export function PublishButton({ resourceId, objectPath, openTab }: { resourceId?: string | null; objectPath?: string; openTab: OpenTab }) {
  const [state, setState] = useState<{ assetId: string; info: AssetProduct } | null>(null);
  useEffect(() => {
    if (!resourceId) return;
    let alive = true;
    api.catalog.resourceAsset(resourceId, objectPath).then((asset) => api.products.forAsset(asset.id).then((info) => alive && setState({ assetId: asset.id, info })))
      .catch(() => alive && setState(null));
    return () => { alive = false; };
  }, [resourceId, objectPath]);
  if (!state || !state.info.publishable) return null;
  if (state.info.product) return <button className="btn secondary" onClick={() => openTab(productTab(state.info.product!))}>In the catalog</button>;
  if (!state.info.can_publish) return null;
  return <button className="btn secondary" onClick={() => openTab({ kind: "products", ref: "mine", title: "My products", intent: "publish", assetId: state.assetId })}>Publish to catalog</button>;
}
