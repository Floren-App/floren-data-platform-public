import { useCallback, useEffect, useState } from "react";
import { api, ApiError, AppFeature, Webhook, WebhookDelivery, AppGroup, AppProfile, rEnabled, AuditEvent, defaultLayerId, DataSource, EDITOR_NAME, Editor, EgressAllowlist, EgressHost, Layer, Pool, Repo, StorageField, StorageType, WorkspaceSettings, WorkspaceTemplate, WorkspaceTemplateInput, WORKLOAD_KINDS, WorkloadKind, usablePools } from "../api";
import type { Hub } from "../hub";
import { OpenTab } from "../components/Permissions";
import { AlertRules } from "./Alerts";
import { PageHeader } from "../components/Page";
import { ProductIcon } from "../components/ProductIcon";
import { IconCompute, IconPlus, IconTrash, IconWorkspaces } from "../components/Icons";

function useLoad<T>(fn: () => Promise<T>, initial: T) {
  const [data, setData] = useState<T>(initial);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => fn().then((d) => { setData(d); setError(null); }).catch((e: Error) => setError(e.message)), [fn]);
  useEffect(() => { reload(); }, [reload]);
  return { data, error, reload, setError };
}

const TYPE_ICON: Record<StorageType["kind"], string> = { postgres: "postgres", iceberg: "iceberg", s3: "bucket" };
const SSL_MODES = ["", "disable", "allow", "prefer", "require", "verify-ca", "verify-full"];

export function AdminLayers({ hub }: { hub: Hub }) {
  const [draft, setDraft] = useState<Layer | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const save = async (e: React.FormEvent) => {
    e.preventDefault(); if (!draft) return; setBusy(true); setError("");
    const body = { name: draft.name.trim(), description: draft.description, workspaces: draft.workspaces, production: !!draft.production };
    try { if (draft.id) await api.layers.update(draft.id, body); else await api.layers.create(body); await hub.refresh(); setDraft(null); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const makeDefault = async (layer: Layer) => {
    setError("");
    try { await api.layers.setDefault(layer.id); await hub.refresh(); } catch (e) { setError((e as Error).message); }
  };
  return <><PageHeader icon={<ProductIcon kind="layer" size={22} />} title="Layers" subtitle="Global boundaries for workspaces, storage, workloads, and APIs. A matching layer is required in addition to the user's object permissions. There are no built-in layers: create them here or with OpenTofu (fdp_layer)."
    actions={<button className="btn" onClick={() => setDraft({ id: "", name: "", description: "", workspaces: true, production: false })}><IconPlus size={14} />Add layer</button>} />
    {error && <p role="alert" className="error">{error}</p>}
    {draft && <form className="card" onSubmit={save}><h2>{draft.id ? `Edit ${draft.name}` : "New layer"}</h2><div className="arg-grid">
      <div className="field"><label htmlFor="layer-name">Layer name</label><input id="layer-name" required autoFocus maxLength={128} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></div>
      <div className="field"><label htmlFor="layer-description">Description</label><input id="layer-description" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></div></div>
      <label className="row"><input type="checkbox" checked={draft.workspaces} onChange={(e) => setDraft({ ...draft, workspaces: e.target.checked })} />Allow workspace creation and starting</label>
      <p className="hint">People can create private workspaces in enabled layers. This creates no security group and grants no access to data or programs. Existing object permissions still apply.</p>
      <label className="row"><input type="checkbox" checked={!!draft.production} onChange={(e) => setDraft({ ...draft, production: e.target.checked })} />Production layer</label>
      <p className="hint">Personal and group secrets cannot be used here; work in a production layer uses service account secrets.</p>
      <div className="form-actions"><button className="btn" disabled={busy || !draft.name.trim()}>Save layer</button><button type="button" className="btn secondary" onClick={() => setDraft(null)}>Cancel</button></div>
    </form>}
    {!hub.layers.length ? <p className="muted">No layers yet. Nothing can run until an administrator creates one, here or with OpenTofu (fdp_layer).</p>
    : <table aria-label="Layers"><thead><tr><th>Layer</th><th>Workspaces</th><th>Production</th><th>Default</th><th /></tr></thead><tbody>{hub.layers.map((layer) => <tr key={layer.id}>
      <td><strong>{layer.name}</strong><div className="muted">{layer.description}</div></td><td>{layer.workspaces ? "Allowed" : "Disabled"}</td><td>{layer.production ? "Yes" : "—"}</td>
      <td>{layer.default ? <span className="badge" title="Work without a layer goes here">default</span> : <button className="btn sm secondary" onClick={() => void makeDefault(layer)}>Make default</button>}</td>
      <td className="table-actions"><div className="row"><button className="btn sm secondary" onClick={() => setDraft(layer)}>Edit</button><button className="icon-btn" aria-label={`Delete layer ${layer.name}`} onClick={async () => { if (!confirm(`Delete the layer ${layer.name}? Only an unused, non-default layer can be deleted.`)) return; try { await api.layers.remove(layer.id); await hub.refresh(); } catch (e) { setError((e as Error).message); } }}><IconTrash size={14} /></button></div></td></tr>)}</tbody></table>}
    <p className="scope-note">Layer assignments cannot be changed by moving an existing workload or data integration. Deploy a separate instance to promote between layers.</p>
  </>;
}

/** Where an integration points, in one line. */

type Draft = { name: string; description: string; config: Record<string, string>; secrets: Record<string, Record<string, string>>; egress: string };
const emptyDraft: Draft = { name: "", description: "", config: {}, secrets: {}, egress: "" };

function SettingField({ id, f, value, error, onChange, keep }: {
  id: string; f: StorageField; value: string; error?: string; onChange: (v: string) => void; keep?: boolean;
}) {
  return <div className="field">
    <label htmlFor={id}>{f.label}{f.required && !keep ? " *" : ""}</label>
    {f.kind === "choice"
      ? <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
          {SSL_MODES.map((o) => <option key={o} value={o}>{o || "Driver default"}</option>)}
        </select>
      : <input id={id} className={f.kind === "password" || f.kind === "port" ? "" : "mono"}
          type={f.kind === "password" ? "password" : f.kind === "port" ? "number" : "text"} autoComplete="off" value={value}
          placeholder={keep ? "Leave empty to keep the stored value" : f.placeholder || (f.default != null ? String(f.default) : "")}
          aria-invalid={!!error} onChange={(e) => onChange(e.target.value)} />}
    {error ? <span className="field-error">{error}</span> : f.help && <span className="hint">{f.help}</span>}
  </div>;
}

/** Admin: the external databases and buckets Floren Data Platform governs. Nothing here hosts storage. */
/** Add or change a storage connection: its type, location and write-only credentials (platform administrators). */
export function StorageConnectionForm({ hub, existing, onDone, onCancel }: { hub: Hub; existing?: DataSource; onDone: (name: string) => void; onCancel: () => void }) {
  const { data: types } = useLoad(api.admin.storageTypes, [] as StorageType[]);
  const [kind, setKind] = useState<StorageType["kind"] | null>(existing ? existing.kind as StorageType["kind"] : null);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const spec = types.find((t) => t.kind === kind);
  const editing = !!existing;
  useEffect(() => {  // fill the form once the type's fields are known
    if (!spec) return;
    setDraft(existing ? { name: existing.name, description: existing.description, egress: existing.egress.join(", "), secrets: {},
      config: { ...Object.fromEntries(spec.config.map((f) => [f.name, existing.config[f.name] != null ? String(existing.config[f.name]) : ""])), layer_id: String(existing.config.layer_id || hub.layerId) } }
      : { ...emptyDraft, config: Object.fromEntries(spec.config.map((f) => [f.name, f.default != null ? String(f.default) : ""])) });
    requestAnimationFrame(() => document.getElementById(existing ? "si-description" : "si-name")?.focus());
  }, [spec?.kind, existing?.name]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!spec) return;
    setBusy(true); setFieldErrors({}); setError("");
    const config: Record<string, unknown> = {};
    config.layer_id = draft.config.layer_id || hub.layerId;
    for (const f of spec.config) {
      const v = draft.config[f.name]?.trim();
      if (v) config[f.name] = f.kind === "port" ? Number(v) : v;
    }
    // Credentials are write-only: send a group only when something was entered.
    const secrets: Record<string, Record<string, string>> = {};
    for (const g of spec.secrets) if (Object.values(draft.secrets[g.name] ?? {}).some(Boolean)) secrets[g.name] = draft.secrets[g.name];
    try {
      await api.admin.upsertSource({ name: draft.name.trim(), kind: spec.kind, description: draft.description, config,
        egress: spec.kind === "s3" ? draft.egress.split(",").map((x) => x.trim()).filter(Boolean) : [], ...secrets });
      await hub.refresh(); onDone(draft.name.trim());
    } catch (err) {
      setError((err as Error).message);
      if (err instanceof ApiError) setFieldErrors(err.fieldErrors);
    } finally { setBusy(false); }
  };

  if (!spec) return <section className="card" aria-label="Choose a storage type">
    <h3>What kind of storage?</h3>
    <div className="type-choices">{types.map((t) => <button key={t.kind} className="type-choice" onClick={() => setKind(t.kind)}>
      <ProductIcon kind={TYPE_ICON[t.kind]} size={22} /><span><strong>{t.label}</strong><span className="muted">{t.summary}</span></span>
    </button>)}</div>
    <div className="form-actions"><button className="btn secondary" onClick={onCancel}>Cancel</button></div>
  </section>;
  return <form className="card" onSubmit={save} aria-label={editing ? `Settings of ${draft.name}` : `New ${spec.label} connection`}>
    <div className="section-heading" style={{ marginTop: 0 }}>
      <h3 className="row" style={{ margin: 0 }}><ProductIcon kind={TYPE_ICON[spec.kind]} size={18} />{editing ? "Connection settings" : `New ${spec.label} connection`}</h3>
      {!editing && <button type="button" className="text-button" onClick={() => setKind(null)}>Change type</button>}
    </div>
    <p className="scope-note"><strong>Configure outside Floren Data Platform first.</strong> {spec.external}</p>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="arg-grid">
      <div className="field"><label htmlFor="si-name">Name *</label>
        <input id="si-name" className="mono" required disabled={editing} pattern="[a-z][a-z0-9-]{1,62}" value={draft.name} placeholder="finance-dw"
          onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        <span className="hint">Lowercase and dashes. Programs connect with this name.</span></div>
      <div className="field"><label htmlFor="si-description">Description</label>
        <input id="si-description" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></div>
    </div>
    <div className="field"><label htmlFor="si-layer">Layer</label><select id="si-layer" required disabled={editing} value={draft.config.layer_id || hub.layerId} onChange={(e) => setDraft({ ...draft, config: { ...draft.config, layer_id: e.target.value } })}>{!(draft.config.layer_id || hub.layerId) && <option value="">Choose a layer</option>}{hub.layers.map((layer) => <option key={layer.id} value={layer.id}>{layer.name}</option>)}</select><span className="hint">All schemas and data objects in this connection inherit its layer.</span></div>
    <h3>Location</h3>
    <div className="arg-grid">{spec.config.map((f) => <SettingField key={f.name} id={`si-${f.name}`} f={f} value={draft.config[f.name] ?? ""} error={fieldErrors[f.name]}
      onChange={(v) => setDraft({ ...draft, config: { ...draft.config, [f.name]: v } })} />)}</div>
    {spec.secrets.map((g) => <section key={g.name}>
      <h3>{g.label}{!g.required && <span className="muted" style={{ fontWeight: 400 }}> · optional</span>}</h3>
      <p className="hint" style={{ marginTop: -4 }}>{g.help}</p>
      <div className="arg-grid">{g.fields.map((f) => <SettingField key={f.name} id={`si-${g.name}-${f.name}`} f={{ ...f, required: g.required }} keep={editing}
        value={draft.secrets[g.name]?.[f.name] ?? ""} error={fieldErrors[`${g.name}.${f.name}`]}
        onChange={(v) => setDraft({ ...draft, secrets: { ...draft.secrets, [g.name]: { ...draft.secrets[g.name], [f.name]: v } } })} />)}</div>
    </section>)}
    {spec.kind === "s3" && <details><summary className="muted">Network access for workloads</summary>
      <div className="field" style={{ marginTop: 8, maxWidth: 720 }}><label htmlFor="si-egress">Direct file-source network (CIDRs)</label>
        <input id="si-egress" className="mono" value={draft.egress} onChange={(e) => setDraft({ ...draft, egress: e.target.value })} placeholder="10.20.0.15/32" />
        <span className="hint">Addresses workloads may reach for this bucket (enforced on Kubernetes).</span></div></details>}
    <div className="form-actions">
      <button className="btn" disabled={busy || !draft.name.trim()}>{busy ? "Saving…" : editing ? "Save changes" : "Create connection"}</button>
      <button type="button" className="btn secondary" onClick={onCancel}>Cancel</button>
    </div>
  </form>;
}

/** A connection's Settings tab: its location and credentials, and removing it (platform administrators). */
export function StorageConnectionSettings({ hub, name, openTab }: { hub: Hub; name: string; openTab: OpenTab }) {
  const { data: sources, error, setError } = useLoad(api.admin.sources, [] as DataSource[]);
  const [message, setMessage] = useState(""), [formKey, setFormKey] = useState(0);
  const ds = sources.find((s) => s.name === name);
  if (!ds) return error ? <p className="error" role="alert">{error}</p> : <p className="muted" role="status">Loading…</p>;
  return <>
    {message && <p className="success-message" role="status">{message}</p>}
    {ds.groups.length > 0 && <p className="scope-note">Legacy grants to review: {ds.groups.join(", ")}</p>}
    {ds.config.system_metadata ? <p className="scope-note">Managed by the deployment; change it in the Helm values.</p>
      : <StorageConnectionForm key={formKey} hub={hub} existing={ds} onDone={() => { setMessage("Saved."); setFormKey((k) => k + 1); }} onCancel={() => setFormKey((k) => k + 1)} />}
    {!ds.config.system_metadata && <section className="card danger-zone"><h3>Remove this connection</h3>
      <p className="muted">Removes the connection, its schemas' registrations and its stored credentials. The storage itself is not touched.</p>
      <button className="btn danger" onClick={() => confirm(`Remove the ${ds.name} connection and its stored credentials? The storage itself is not touched.`)
        && api.admin.deleteSource(ds.name).then(async () => { await hub.refresh(); openTab({ kind: "data", title: "Storage" }); }).catch((e: Error) => setError(e.message))}>Remove connection</button>
      {error && <p className="error" role="alert">{error}</p>}</section>}
  </>;
}

export function AdminPools({ hub }: { hub: Hub }) {
  const { data: pools, error, reload, setError } = useLoad(api.admin.pools, [] as Pool[]);
  const [form, setForm] = useState<Omit<Pool, "id"> | null>(null);

  const save = async () => {
    if (!form) return;
    try {
      await api.admin.upsertPool(form);
      setForm(null);
      reload();
      hub.refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const num = (k: keyof Omit<Pool, "id">) => (e: React.ChangeEvent<HTMLInputElement>) => form && setForm({ ...form, [k]: Number(e.target.value) });

  return (
    <>
      <PageHeader icon={<IconCompute size={22} />} title="Compute pools"
        subtitle="Sized like warehouses. Every job, deployed app and Airflow component requests exactly its pool size and takes one slot; work beyond Max concurrent waits in line. The cluster autoscaler adds and removes nodes."
        actions={<button className="btn" aria-expanded={!!form && !pools.some((p) => p.name === form.name)} onClick={() => setForm({ name: "", cpu: 2, memory_gb: 8, gpu: 0, max_concurrent: 10, auto_suspend_seconds: 600, allowed_groups: [] })}><IconPlus size={14} />Add pool</button>} />
      {error && <p className="error" role="alert">{error}</p>}
      {form && (
        <form className="card" onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <h3>{pools.some((p) => p.name === form.name) ? `Edit ${form.name}` : "Add pool"}</h3>
          <div className="arg-grid">
            <div className="field"><label htmlFor="p-name">Name</label><input id="p-name" className="mono" autoFocus value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
            <div className="field"><label htmlFor="p-cpu">vCPU</label><input id="p-cpu" type="number" min={0.25} step={0.25} value={form.cpu} onChange={num("cpu")} /></div>
            <div className="field"><label htmlFor="p-mem">Memory GB</label><input id="p-mem" type="number" min={1} value={form.memory_gb} onChange={num("memory_gb")} /></div>
            <div className="field"><label htmlFor="p-gpu">GPU</label><input id="p-gpu" type="number" min={0} value={form.gpu} onChange={num("gpu")} /></div>
            <div className="field"><label htmlFor="p-max" title="Jobs, deployed apps, Airflow components and Trino workers running at once; more wait in line">Max concurrent</label><input id="p-max" type="number" min={1} disabled={!!form.dedicated} value={form.max_concurrent} onChange={num("max_concurrent")} />{form.dedicated && <small className="muted">No limit: dedicated</small>}</div>
            <div className="field"><label htmlFor="p-sus">Auto-suspend (s)</label><input id="p-sus" type="number" min={60} value={form.auto_suspend_seconds} onChange={num("auto_suspend_seconds")} /></div>
          </div>
          <div className="field"><label htmlFor="p-groups">Allowed groups {form.dedicated ? "(required)" : "(empty = everyone)"}</label>
            <input id="p-groups" value={form.allowed_groups.join(", ")} onChange={(e) => setForm({ ...form, allowed_groups: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })} /></div>
          <label className="row"><input type="checkbox" checked={!!form.dedicated} onChange={(e) => setForm({ ...form, dedicated: e.target.checked })} />
            Dedicated to these groups: not a shared on-demand warehouse, no slot limit and never waits in line. Make sure the cluster has nodes for it.</label>
          <fieldset className="field"><legend>For layers <small className="muted">(none checked = every layer)</small></legend>
            <div className="row wrap">{hub.layers.map((layer) => <label key={layer.id} className="row"><input type="checkbox"
              checked={!!form.layer_ids?.includes(layer.id)} onChange={(e) => setForm({ ...form, layer_ids: e.target.checked
                ? [...(form.layer_ids ?? []), layer.id] : (form.layer_ids ?? []).filter((id) => id !== layer.id) })} />{layer.name}</label>)}</div>
          </fieldset>
          <fieldset className="field"><legend>For kinds of work <small className="muted">(none checked = every kind)</small></legend>
            <div className="row wrap">{WORKLOAD_KINDS.map((kind) => <label key={kind} className="row"><input type="checkbox"
              checked={!!form.workload_kinds?.includes(kind)} onChange={(e) => setForm({ ...form, workload_kinds: e.target.checked
                ? [...(form.workload_kinds ?? []), kind] : (form.workload_kinds ?? []).filter((k) => k !== kind) })} />{kind}</label>)}</div>
            <small className="muted">With groups, this says e.g. "dbt projects of the data engineers in Prod". Work outside the scope is refused here; a program that declares this warehouse falls back to the default for its layer and kind.</small>
          </fieldset>
          <div className="form-actions">
            <button className="btn" disabled={!form.name || (!!form.dedicated && !form.allowed_groups.length)}>Save</button>
            <button type="button" className="btn secondary" onClick={() => setForm(null)}>Cancel</button>
          </div>
        </form>
      )}
      <table>
        <thead><tr><th>Name</th><th>vCPU</th><th>Memory</th><th>GPU</th><th>In use / max</th><th>Auto-suspend</th><th>Groups</th><th>For</th><th /></tr></thead>
        <tbody>
          {pools.map((p) => (
            <tr key={p.id}>
              <td><b>{p.name}</b>{p.default && <span className="badge" title="Work without a warehouse runs here">default</span>}
                {!!p.trino_layers?.length && <span className="badge" title={`Trino workers of ${p.trino_layers.join(", ")}; only Trino uses it`}>Trino · {p.trino_layers.join(", ")}</span>}
                {p.default_for?.map((d) => <span key={`${d.layer_id}/${d.workload_kind}`} className="badge" title="Default for this work">default · {[d.layer_name, d.workload_kind].filter(Boolean).join(" · ")}</span>)}</td><td>{p.cpu}</td><td>{p.memory_gb} GB</td><td>{p.gpu}</td><td>{p.dedicated ? `${p.in_use ?? 0} · dedicated, no limit` : `${p.in_use ?? 0} / ${p.max_concurrent}${p.queued ? ` · ${p.queued} waiting` : ""}`}</td>
              <td>{Math.round(p.auto_suspend_seconds / 60)} min</td><td>{p.allowed_groups.join(", ") || "everyone"}</td>
              <td>{[p.layer_names?.join(", ") || "every layer", p.workload_kinds?.join(", ") || "every kind"].join(" · ")}</td>
              <td className="table-actions"><div className="row">
                {!p.default && !p.dedicated && !p.trino_layers?.length && !p.layer_ids?.length && !p.workload_kinds?.length && <button className="btn sm secondary" onClick={() => void api.admin.setDefaultWarehouse(p.name).then(() => { reload(); hub.refresh(); }).catch((e: Error) => setError(e.message))}>Make default</button>}
                <button className="btn sm secondary" onClick={() => { const { id: _id, ...rest } = p; setForm(rest); }}>Edit</button></div></td>
            </tr>
          ))}
        </tbody>
      </table>
      <ScopedDefaults hub={hub} pools={pools} onChanged={() => { reload(); hub.refresh(); }} />
      <WorkspaceLimits pools={pools} />
    </>
  );
}

/** Defaults per layer and/or kind of work: the most specific match wins (layer and kind, the layer, the kind). */
function ScopedDefaults({ hub, pools, onChanged }: { hub: Hub; pools: Pool[]; onChanged: () => void }) {
  const [layerId, setLayerId] = useState("");
  const [kind, setKind] = useState<WorkloadKind | "">("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const entries = pools.flatMap((p) => (p.default_for ?? []).map((d) => ({ ...d, name: p.name })));
  const choices = usablePools(pools, layerId || null, kind || null).filter((p) => !p.dedicated);
  const act = (work: Promise<unknown>) => void work.then(() => { setError(""); onChanged(); }).catch((e: Error) => setError(e.message));
  return <section className="card">
    <h3>Defaults by layer and kind of work</h3>
    <p className="muted">Work that names no warehouse, or names one not meant for it, runs on the most specific default: for its layer and kind, its layer, its kind, then the default above.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <table><thead><tr><th>Layer</th><th>Kind</th><th>Warehouse</th><th /></tr></thead><tbody>
      {entries.map((d) => <tr key={`${d.layer_id}/${d.workload_kind}`}><td>{d.layer_name ?? "any"}</td><td>{d.workload_kind ?? "any"}</td><td>{d.name}</td>
        <td className="table-actions"><button className="btn sm secondary" onClick={() => act(api.admin.clearDefaultWarehouse({ layer_id: d.layer_id, workload_kind: d.workload_kind }))}>Clear</button></td></tr>)}
      {!entries.length && <tr><td colSpan={4} className="muted">None: work without a warehouse uses the default above.</td></tr>}
    </tbody></table>
    <form className="row wrap" onSubmit={(e) => { e.preventDefault(); act(api.admin.setDefaultWarehouse(name, { ...(layerId ? { layer_id: layerId } : {}), ...(kind ? { workload_kind: kind } : {}) })); }}>
      <label className="field">Layer<select value={layerId} onChange={(e) => setLayerId(e.target.value)}><option value="">any</option>{hub.layers.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select></label>
      <label className="field">Kind<select value={kind} onChange={(e) => setKind(e.target.value as WorkloadKind | "")}><option value="">any</option>{WORKLOAD_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}</select></label>
      <label className="field">Warehouse<select value={name} onChange={(e) => setName(e.target.value)}><option value="">— choose —</option>{choices.map((p) => <option key={p.id} value={p.name}>{p.name}</option>)}</select></label>
      <button className="btn" disabled={!name || (!layerId && !kind)}>Set default</button>
    </form>
  </section>;
}

function WorkspaceLimits({ pools }: { pools: Pool[] }) {
  const { data: settings, error, reload, setError } = useLoad(api.admin.workspaceSettings, null as WorkspaceSettings | null);
  const [draft, setDraft] = useState<WorkspaceSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const value = draft ?? settings;
  if (!value) return error ? <p className="error" role="alert">{error}</p> : null;
  const change = (patch: Partial<WorkspaceSettings>) => { setSaved(false); setDraft({ ...value, ...patch }); };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try { await api.admin.saveWorkspaceSettings(value); setDraft(null); setSaved(true); reload(); }
    catch (err) { setError((err as Error).message); }
  };
  return (
    <form className="card" aria-label="Workspace limits" onSubmit={(e) => void save(e)}>
      <h3>Workspaces</h3>
      <p className="muted">Workspaces have their own limits and never take warehouse slots. {settings?.running ?? 0} running{settings?.queued ? `, ${settings.queued} waiting` : ""}.</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="arg-grid">
        <div className="field"><label htmlFor="ws-warehouse">Editor size (warehouse)</label>
          <select id="ws-warehouse" value={value.warehouse ?? ""} onChange={(e) => change({ warehouse: e.target.value || null })}>
            <option value="">Installation default</option>
            {pools.filter((p) => !p.dedicated && !p.trino_layers?.length).map((p) => <option key={p.id} value={p.name}>{p.name} · {p.cpu} vCPU · {p.memory_gb} GB</option>)}
          </select></div>
        <div className="field"><label htmlFor="ws-total">Running at once (everyone)</label>
          <input id="ws-total" type="number" min={1} placeholder="No limit" value={value.max_running ?? ""}
                 onChange={(e) => change({ max_running: e.target.value ? Number(e.target.value) : null })} /></div>
        <div className="field"><label htmlFor="ws-person">Per person</label>
          <input id="ws-person" type="number" min={1} required value={value.max_running_per_user}
                 onChange={(e) => change({ max_running_per_user: Number(e.target.value) })} /></div>
      </div>
      <p className="muted">Beyond the total, workspaces wait in line and start in order. Per person counts running and waiting workspaces.</p>
      <div className="form-actions">
        <button className="btn" disabled={!draft}>Save</button>
        {draft && <button type="button" className="btn secondary" onClick={() => setDraft(null)}>Cancel</button>}
        {saved && <span className="muted" role="status">Saved</span>}
      </div>
    </form>
  );
}

export function AdminAudit({ initialActor = "", initialTarget = "" }: { initialActor?: string; initialTarget?: string }) {
  const [filters, setFilters] = useState({ actor: initialActor, action: "", target: initialTarget });
  const [events, setEvents] = useState<AuditEvent[]>([]), [error, setError] = useState("");
  useEffect(() => {  // filter as people type, without a request per keystroke
    const t = setTimeout(() => api.admin.audit(filters).then((e) => { setEvents(e); setError(""); }).catch((e: Error) => setError(e.message)), 250);
    return () => clearTimeout(t);
  }, [filters]);
  return (
    <>
      <PageHeader title="Audit log" subtitle="Who changed what: every data connection (and denial), grant, workspace start, run and catalog change, newest first." />
      <div className="arg-grid" role="search" aria-label="Filter the audit log">
        <div className="field"><label htmlFor="audit-actor">Who</label><input id="audit-actor" value={filters.actor} onChange={(e) => setFilters({ ...filters, actor: e.target.value })} placeholder="person or service" /></div>
        <div className="field"><label htmlFor="audit-action">Action</label><input id="audit-action" value={filters.action} onChange={(e) => setFilters({ ...filters, action: e.target.value })} placeholder="rbac, compliance, workspace…" /></div>
        <div className="field"><label htmlFor="audit-target">Object ID</label><input id="audit-target" className="mono" value={filters.target} onChange={(e) => setFilters({ ...filters, target: e.target.value })} /></div>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <table aria-label="Audit events">
        <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
        <tbody>
          {events.map((e, i) => (
            <tr key={i}>
              <td className="muted" style={{ whiteSpace: "nowrap" }}>{new Date(e.at).toLocaleString()}</td><td>{e.actor}</td><td className="mono">{e.action}</td><td>{e.target}</td>
              <td className="mono muted" style={{ overflowWrap: "anywhere" }}>{JSON.stringify(e.detail)}</td>
            </tr>
          ))}
          {!events.length && <tr><td colSpan={5} className="muted">No events match.</td></tr>}
        </tbody>
      </table>
    </>
  );
}

/** Admin: group workspace templates. Every member of the group gets their own private, stopped workspace. */
export function AdminWorkspaceTemplates({ hub }: { hub: Hub }) {
  const { data: templates, error, reload, setError } = useLoad(api.admin.workspaceTemplates, [] as WorkspaceTemplate[]);
  const { data: groups } = useLoad(api.access.groups, [] as AppGroup[]);
  const { data: repos } = useLoad(api.admin.repos, [] as Repo[]);
  const layers = hub.layers.filter((l) => l.workspaces);
  const blank = (): WorkspaceTemplateInput => ({ name: "", group_id: groups[0]?.id ?? "", editor: "vscode",
    layer_id: (layers.some((l) => l.id === hub.layerId) ? hub.layerId : defaultLayerId(layers)) || layers[0]?.id || "", description: "", repositories: [] });
  const [draft, setDraft] = useState<(WorkspaceTemplateInput & { id?: string }) | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async (e: React.FormEvent) => {
    e.preventDefault(); if (!draft) return; setBusy(true); setError(null);
    const { id, ...body } = draft;
    try { if (id) await api.admin.updateWorkspaceTemplate(id, body); else await api.admin.createWorkspaceTemplate(body); setDraft(null); reload(); hub.refresh(); }
    catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  return <>
    <PageHeader icon={<IconWorkspaces size={22} />} title="Workspace templates"
      subtitle="Defaults per group: every member gets their own private workspace from each template of their group, created stopped (no cost until first start), also people who join later. Leaving the group removes theirs; files stay in its volume. Also configurable with OpenTofu (fdp_workspace_template)."
      actions={<button className="btn" onClick={() => setDraft(blank())}><IconPlus size={14} />Add template</button>} />
    {error && <p className="error" role="alert">{error}</p>}
    {draft && <form className="card" aria-label="Workspace template" onSubmit={(e) => void save(e)}>
      <h3>{draft.id ? `Edit ${draft.name}` : "New template"}</h3>
      <div className="arg-grid">
        <div className="field"><label htmlFor="wt-name">Workspace name</label><input id="wt-name" required maxLength={64} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></div>
        <div className="field"><label htmlFor="wt-group">Group</label><select id="wt-group" required value={draft.group_id} onChange={(e) => setDraft({ ...draft, group_id: e.target.value })}>
          {!draft.group_id && <option value="">Choose a group</option>}{groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select></div>
        <div className="field"><label htmlFor="wt-editor">Editor</label><select id="wt-editor" value={draft.editor} onChange={(e) => setDraft({ ...draft, editor: e.target.value as Editor })}>
          {(Object.keys(EDITOR_NAME) as Editor[]).filter((e) => e !== "rstudio" || rEnabled(hub.me)).map((e) => <option key={e} value={e}>{EDITOR_NAME[e]}</option>)}</select></div>
        <div className="field"><label htmlFor="wt-layer">Layer</label><select id="wt-layer" required value={draft.layer_id} onChange={(e) => setDraft({ ...draft, layer_id: e.target.value })}>
          {!draft.layer_id && <option value="">Choose a layer</option>}{layers.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select></div>
      </div>
      <div className="field"><label htmlFor="wt-description">Description</label><input id="wt-description" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></div>
      {!!repos.length && <fieldset className="plain"><legend>Repositories cloned at first start</legend>
        {repos.map((r) => <label key={r.id} className="row"><input type="checkbox" checked={draft.repositories.includes(r.id)}
          onChange={(e) => setDraft({ ...draft, repositories: e.target.checked ? [...draft.repositories, r.id] : draft.repositories.filter((x) => x !== r.id) })} />{r.name}</label>)}
      </fieldset>}
      <div className="form-actions"><button className="btn" disabled={busy || !draft.name.trim() || !draft.group_id || !draft.layer_id}>Save template</button>
        <button type="button" className="btn secondary" onClick={() => setDraft(null)}>Cancel</button></div>
    </form>}
    {!templates.length ? <p className="muted">No templates yet. Members of a group then create their own workspaces.</p>
    : <table aria-label="Workspace templates"><thead><tr><th>Template</th><th>Group</th><th>Editor</th><th>Layer</th><th>Workspaces</th><th /></tr></thead><tbody>
      {templates.map((t) => <tr key={t.id}><td><strong>{t.name}</strong><div className="muted">{t.description}</div></td><td>{t.group_name ?? t.group_id}</td>
        <td>{EDITOR_NAME[t.editor]}</td><td>{t.layer_name ?? t.layer_id}</td><td>{t.workspaces ?? 0}</td>
        <td className="table-actions"><div className="row"><button className="btn sm secondary" onClick={() => setDraft({ id: t.id, name: t.name, group_id: t.group_id, editor: t.editor, layer_id: t.layer_id, description: t.description, repositories: t.repositories })}>Edit</button>
          <button className="icon-btn" aria-label={`Delete template ${t.name}`} onClick={() => { if (confirm(`Delete ${t.name}? Its members' workspaces are removed; their files stay.`)) void api.admin.deleteWorkspaceTemplate(t.id).then(() => { reload(); hub.refresh(); }).catch((e: Error) => setError(e.message)); }}><IconTrash size={14} /></button></div></td></tr>)}
    </tbody></table>}
  </>;
}

export function AdminNetwork() {
  const { data, error, reload, setError } = useLoad(api.admin.egress, { hosts: [], proxy: true } as EgressAllowlist);
  const [draft, setDraft] = useState<EgressHost[] | null>(null), [busy, setBusy] = useState(false);
  const hosts = draft ?? data.hosts;
  const edit = (i: number, change: Partial<EgressHost>) => setDraft(hosts.map((h, n) => n === i ? { ...h, ...change } : h));
  const save = async () => {
    setBusy(true); setError(null);
    try { await api.admin.saveEgress(hosts.filter((h) => h.host.trim())); setDraft(null); reload(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return <>
    <PageHeader icon={<ProductIcon kind="identity" size={22} />} title="Network allowlist"
      subtitle="Host names VS Code and RStudio workspaces may reach over HTTPS, for example github.com for Git, or *.githubusercontent.com for its subdomains. Nothing else on the internet is reachable, and names that resolve to private addresses are refused. Changes reach running workspaces within seconds. Also configurable with OpenTofu (fdp_egress_allowlist)."
      actions={<button className="btn" onClick={() => setDraft([...hosts, { host: "", description: "" }])}><IconPlus size={14} />Add host</button>} />
    {error && <p className="error" role="alert">{error}</p>}
    {!data.proxy && <p className="error" role="alert">This installation does not run the egress proxy (Helm egressProxy.enabled), so workspaces have no internet access whatever this list says.</p>}
    {!hosts.length ? <p className="muted">No hosts allowed: workspaces reach only the platform.</p>
    : <table aria-label="Allowed hosts"><thead><tr><th>Host name</th><th>Description</th><th /></tr></thead><tbody>
      {hosts.map((h, i) => <tr key={i}>
        <td><input aria-label={`Host ${i + 1}`} placeholder="github.com" value={h.host} onChange={(e) => edit(i, { host: e.target.value })} /></td>
        <td><input aria-label={`Description ${i + 1}`} maxLength={200} value={h.description} onChange={(e) => edit(i, { description: e.target.value })} /></td>
        <td className="table-actions"><button className="icon-btn" aria-label={`Remove ${h.host || "host"}`} onClick={() => setDraft(hosts.filter((_, n) => n !== i))}><IconTrash size={14} /></button></td>
      </tr>)}
    </tbody></table>}
    {draft && <div className="form-actions"><button className="btn" disabled={busy} onClick={() => void save()}>Save allowlist</button>
      <button className="btn secondary" onClick={() => { setDraft(null); setError(null); }}>Cancel</button></div>}
  </>;
}

/** App profiles: which parts of the app each group gets. A grid of features against profiles. */
export function AdminAppProfiles() {
  const [data, setData] = useState<{ profiles: AppProfile[]; features: { key: AppFeature; label: string }[] } | null>(null);
  const [drafts, setDrafts] = useState<Record<string, AppProfile>>({}), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [newName, setNewName] = useState("");
  const load = useCallback(() => api.appProfiles.list().then((d) => { setData(d); setDrafts(Object.fromEntries(d.profiles.map((p) => [p.id, p]))); })
    .catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void load(); }, [load]);
  if (!data) return error ? <p className="error" role="alert">{error}</p> : <p className="muted" role="status">Loading…</p>;
  const profiles = data.profiles;
  const changed = (p: AppProfile) => JSON.stringify(drafts[p.id]) !== JSON.stringify(p);
  const toggle = (p: AppProfile, f: AppFeature) => { const d = drafts[p.id];
    setDrafts({ ...drafts, [p.id]: { ...d, features: d.features.includes(f) ? d.features.filter((x) => x !== f) : [...d.features, f] } }); };
  const save = (p: AppProfile) => { const d = drafts[p.id]; setError(""); setMessage("");
    api.appProfiles.put(p.name, { name: d.name.trim(), description: d.description, features: d.features, default: d.default })
      .then(() => { setMessage(`Saved ${d.name}.`); return load(); }).catch((e: Error) => setError(e.message)); };
  return <>
    <PageHeader icon={<ProductIcon kind="groups" size={22} />} title="App profiles"
      subtitle="Which parts of the app a group gets: pages, navigation and the actions behind them, enforced by the platform. People get the profile of their active group; object permissions still decide what they may open. Global Admin always has everything." />
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="scope-note" role="status">{message}</p>}
    <div className="table-scroll"><table className="profile-grid" aria-label="App profiles">
      <thead><tr><th>Feature</th>{profiles.map((p) => <th key={p.id}>
        <input aria-label={`Name of ${p.name}`} value={drafts[p.id]?.name ?? p.name} onChange={(e) => setDrafts({ ...drafts, [p.id]: { ...drafts[p.id], name: e.target.value } })} />
        <label className="row muted"><input type="checkbox" checked={drafts[p.id]?.default ?? p.default}
          onChange={(e) => setDrafts(Object.fromEntries(Object.entries(drafts).map(([id, d]) => [id, { ...d, default: id === p.id ? e.target.checked : e.target.checked ? false : d.default }])))} />Default</label></th>)}</tr></thead>
      <tbody>{data.features.map((f) => <tr key={f.key}><td>{f.label}</td>{profiles.map((p) => <td key={p.id}>
        <input type="checkbox" aria-label={`${f.label} in ${p.name}`} checked={drafts[p.id]?.features.includes(f.key) ?? false} onChange={() => toggle(p, f.key)} /></td>)}</tr>)}
        <tr><td className="muted">Groups</td>{profiles.map((p) => <td key={p.id} className="muted small">{p.groups.join(", ") || "—"}</td>)}</tr>
        <tr><td /> {profiles.map((p) => <td key={p.id}><div className="row" style={{ gap: 4 }}>
          <button className="btn sm" disabled={!changed(p)} onClick={() => save(p)}>Save</button>
          {!p.groups.length && <button className="btn sm ghost danger" onClick={() => void api.appProfiles.remove(p.name).then(load).catch((e: Error) => setError(e.message))}>Remove</button>}</div></td>)}</tr>
      </tbody></table></div>
    {!profiles.length && <p className="muted">No app profiles yet: every group gets the whole app, and object permissions decide what people may open.</p>}
    <p className="hint">A feature brings what it needs: requesting access brings browsing products, deploying workloads brings viewing them. Groups without a profile get the default profile, or the whole app when there is none. Give groups a profile on their page under Security → App groups. Also configurable with Terraform (fdp_app_profile).</p>
    <form className="row" style={{ gap: 8, marginTop: 12 }} onSubmit={(e) => { e.preventDefault(); setError("");
      api.appProfiles.put(newName.trim(), { name: newName.trim(), description: "", features: ["products.browse"], default: false }).then(() => { setNewName(""); return load(); }).catch((err: Error) => setError(err.message)); }}>
      <input aria-label="New profile name" placeholder="New profile name" value={newName} onChange={(e) => setNewName(e.target.value)} />
      <button className="btn secondary" disabled={!newName.trim()}>Add profile</button></form>
  </>;
}

/** The classifications, from least to most sensitive, with what masked data of each counts as. */
export function AdminClassifications() {
  const [rows, setRows] = useState<{ name: string; masked_as: string; description: string }[] | null>(null);
  const [error, setError] = useState(""), [message, setMessage] = useState("");
  const load = useCallback(() => api.catalog.classifications().then((r) => setRows(r.levels.map((l) => ({ ...l })))).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void load(); }, [load]);
  if (!rows) return error ? <p className="error" role="alert">{error}</p> : <p className="muted" role="status">Loading…</p>;
  const set = (i: number, patch: Partial<(typeof rows)[number]>) => setRows(rows.map((r, j) => j === i ? { ...r, ...patch } : r));
  const move = (i: number, by: number) => { const next = [...rows]; [next[i], next[i + by]] = [next[i + by], next[i]]; setRows(next); };
  const save = () => { setError(""); setMessage("");
    api.catalog.putClassifications(rows.map((r) => ({ ...r, name: r.name.trim(), masked_as: r.masked_as || r.name.trim() })))
      .then((r) => { setRows(r.levels); setMessage("Saved classifications."); }).catch((e: Error) => setError(e.message)); };
  return <>
    <PageHeader icon={<ProductIcon kind="identity" size={22} />} title="Classifications"
      subtitle="From least to most sensitive. Stewards classify columns and objects with these; an object is never below its columns. Masked data of a level counts as its masked level when access is decided, so masked personal data can need less than the real values." />
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="scope-note" role="status">{message}</p>}
    <table aria-label="Classifications"><thead><tr><th>Order</th><th>Name</th><th>Masked data counts as</th><th>Description</th><th /></tr></thead><tbody>
      {rows.map((r, i) => <tr key={i}>
        <td><div className="row" style={{ gap: 2 }}><button className="icon-btn" aria-label={`Move ${r.name} up`} disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
          <button className="icon-btn" aria-label={`Move ${r.name} down`} disabled={i === rows.length - 1} onClick={() => move(i, 1)}>↓</button></div></td>
        <td><input aria-label={`Name of level ${i + 1}`} value={r.name} onChange={(e) => set(i, { name: e.target.value.toLowerCase() })} /></td>
        <td><select aria-label={`Masked ${r.name} counts as`} value={r.masked_as || r.name} onChange={(e) => set(i, { masked_as: e.target.value })}>
          {rows.slice(0, i + 1).map((l) => <option key={l.name} value={l.name}>{l.name === r.name ? `${l.name} (itself)` : l.name}</option>)}</select></td>
        <td><input aria-label={`Description of ${r.name}`} value={r.description} onChange={(e) => set(i, { description: e.target.value })} /></td>
        <td><button className="icon-btn" aria-label={`Remove ${r.name}`} onClick={() => setRows(rows.filter((_, j) => j !== i))}><IconTrash size={14} /></button></td>
      </tr>)}
      {!rows.length && <tr><td colSpan={5} className="muted">No classifications yet: everything is unclassified, and no access policy applies.</td></tr>}</tbody></table>
    <div className="form-actions"><button className="btn secondary" onClick={() => setRows([...rows, { name: "", masked_as: "", description: "" }])}><IconPlus size={14} />Add level</button>
      <button className="btn" onClick={save}>Save classifications</button></div>
    <p className="hint">A level still used by a column or object cannot be removed. Also configurable with Terraform (fdp_classifications).</p>
  </>;
}

/** Notifications: webhooks the platform posts its events to, for example a chat channel or automation that sends email. */
export function AdminNotifications({ hub }: { hub: Hub }) {
  const [data, setData] = useState<{ webhooks: Webhook[]; events: { key: string; label: string }[] } | null>(null);
  const [error, setError] = useState(""), [message, setMessage] = useState("");
  const [editing, setEditing] = useState<{ current: string; name: string; url: string; events: string[]; enabled: boolean; secret: string; remove: boolean; has: boolean } | null>(null);
  const [deliveries, setDeliveries] = useState<{ name: string; items: WebhookDelivery[] } | null>(null);
  const load = useCallback(() => api.webhooks.list().then(setData).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void load(); }, [load]);
  if (!data) return error ? <p className="error" role="alert">{error}</p> : <p className="muted" role="status">Loading…</p>;
  const label = (key: string) => data.events.find((e) => e.key === key)?.label ?? key;
  const done = (text: string) => { setMessage(text); setEditing(null); void load(); };
  const save = (e: React.FormEvent) => { e.preventDefault(); if (!editing) return; setError(""); setMessage("");
    api.webhooks.put(editing.current || editing.name.trim(), { name: editing.name.trim(), url: editing.url.trim(), events: editing.events, enabled: editing.enabled,
      ...(editing.secret ? { secret: editing.secret } : {}), ...(editing.remove ? { remove_secret: true } : {}) })
      .then(() => done(`Saved ${editing.name.trim()}.`)).catch((err: Error) => setError(err.message)); };
  const test = (h: Webhook) => { setError(""); setMessage("");
    api.webhooks.test(h.name).then((r) => { if (r.sent) setMessage(`Test sent to ${h.name}.`); else setError(`The test to ${h.name} failed: ${r.error}`); void load(); })
      .catch((e: Error) => setError(e.message)); };
  const show = (h: Webhook) => api.webhooks.deliveries(h.name).then((items) => setDeliveries({ name: h.name, items })).catch((e: Error) => setError(e.message));
  return <>
    <PageHeader icon={<ProductIcon kind="api" size={22} />} title="Alerts"
      subtitle="Channels are where alerts go: a chat channel, or automation that sends the email, posted to as JSON. Rules choose which events go to which channel, for which layers, objects and classifications; owners also add rules on their own objects' Alerts tab. Each message says what happened, who should act (with their email) and links into the app; form answers are never included."
      actions={<button className="btn" onClick={() => setEditing({ current: "", name: "", url: "", events: [], enabled: true, secret: "", remove: false, has: false })}><IconPlus size={14} />Add channel</button>} />
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="scope-note" role="status">{message}</p>}
    <h2>Channels</h2>
    <div className="table-scroll"><table aria-label="Channels"><thead><tr><th>Name</th><th>URL</th><th>Events</th><th>Last message</th><th /></tr></thead><tbody>
      {data.webhooks.map((h) => <tr key={h.id}>
        <td><strong>{h.name}</strong>{!h.enabled && <span className="badge"> Off</span>}{h.has_secret && <div className="muted small">Signed</div>}</td>
        <td className="mono small">{h.url}</td>
        <td className="small">{h.events.length ? h.events.map(label).join(", ") : <span className="muted">What rules send it</span>}</td>
        <td className="small">{h.last ? <>{h.last.event} · {h.last.sent_at ? "delivered" : h.last.error ? <span className="error-text">{h.last.error}</span> : "waiting"}</> : <span className="muted">None yet</span>}
          {h.failing && <div className="error-text">Some messages could not be delivered</div>}</td>
        <td className="table-actions"><div className="row" style={{ gap: 4 }}>
          <button className="btn sm secondary" onClick={() => test(h)}>Send test</button>
          <button className="btn sm secondary" onClick={() => void show(h)}>Messages</button>
          <button className="btn sm secondary" onClick={() => setEditing({ current: h.name, name: h.name, url: h.url, events: h.events, enabled: h.enabled, secret: "", remove: false, has: h.has_secret })}>Edit</button>
          <button className="icon-btn" aria-label={`Remove ${h.name}`} onClick={() => void api.webhooks.remove(h.name).then(() => done(`Removed ${h.name}.`)).catch((e: Error) => setError(e.message))}><IconTrash size={14} /></button></div></td>
      </tr>)}
      {!data.webhooks.length && <tr><td colSpan={5} className="muted">No channels yet: the app alone shows what waits for people.</td></tr>}</tbody></table></div>
    {editing && <form className="card" aria-label={editing.current ? `Edit ${editing.current}` : "New channel"} onSubmit={save}>
      <h2>{editing.current ? `Edit ${editing.current}` : "New channel"}</h2>
      <div className="arg-grid">
        <div className="field"><label htmlFor="hook-name">Name</label><input id="hook-name" value={editing.name} maxLength={120} onChange={(e) => setEditing({ ...editing, name: e.target.value })} /></div>
        <div className="field"><label htmlFor="hook-url">URL</label><input id="hook-url" value={editing.url} placeholder="https://" onChange={(e) => setEditing({ ...editing, url: e.target.value })} /></div>
        <div className="field"><label htmlFor="hook-secret">Signing secret{editing.has && " (set; type to replace)"}</label>
          <input id="hook-secret" type="password" autoComplete="new-password" value={editing.secret} onChange={(e) => setEditing({ ...editing, secret: e.target.value, remove: false })} />
          <span className="hint">Optional, at least 16 characters. Messages then carry X-FDP-Signature: sha256=HMAC of the body. Kept in the secret store.</span>
          {editing.has && <label className="row"><input type="checkbox" checked={editing.remove} onChange={(e) => setEditing({ ...editing, remove: e.target.checked, secret: "" })} />Remove the secret</label>}</div>
        <label className="row"><input type="checkbox" checked={editing.enabled} onChange={(e) => setEditing({ ...editing, enabled: e.target.checked })} />Enabled</label>
      </div>
      <fieldset className="field form-choices"><legend>Events this channel always gets</legend><span className="hint">None chosen: only what alert rules send it (below).</span>
        {data.events.map((ev) => <label key={ev.key} className="row"><input type="checkbox" checked={editing.events.includes(ev.key)}
          onChange={(e) => setEditing({ ...editing, events: e.target.checked ? [...editing.events, ev.key] : editing.events.filter((x) => x !== ev.key) })} />
          {ev.label} <span className="muted mono small">{ev.key}</span></label>)}</fieldset>
      <div className="form-actions"><button className="btn" disabled={!editing.name.trim() || !editing.url.trim()}>Save channel</button>
        <button type="button" className="btn secondary" onClick={() => setEditing(null)}>Cancel</button></div>
    </form>}
    {deliveries && <section className="card" aria-label={`Messages to ${deliveries.name}`}><h2>Messages to {deliveries.name}</h2>
      <div className="table-scroll"><table><thead><tr><th>Event</th><th>Subject</th><th>Created</th><th>Result</th></tr></thead><tbody>
        {deliveries.items.map((d) => <tr key={d.id}><td className="mono small">{d.event}</td><td>{d.subject}</td><td>{new Date(d.created_at).toLocaleString()}</td>
          <td>{d.sent_at ? "Delivered" : d.error ? <span className="error-text">{d.error} ({d.attempts} attempts)</span> : "Waiting"}</td></tr>)}
        {!deliveries.items.length && <tr><td colSpan={4} className="muted">No messages yet.</td></tr>}</tbody></table></div>
      <button className="btn secondary" onClick={() => setDeliveries(null)}>Close</button></section>}
    <h2>Rules</h2>
    <AlertRules hub={hub} />
    <p className="hint">Also configurable with Terraform: fdp_alert_channel and fdp_alert_rule.</p>
  </>;
}
