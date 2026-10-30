import { useCallback, useEffect, useState } from "react";
import { api, ApiError, CertificateInfo, Runtime, RuntimeKind, runtimeLabel, Secret, SecretInput, ServiceAccount, ServiceToken } from "../api";
import type { Hub } from "../hub";
import { IconClose, IconKey, IconPlus, IconTrash } from "../components/Icons";
import { PageHeader } from "../components/Page";
import { OpenTab, resourceTab } from "../components/Permissions";
import { useCached } from "../cache";
import { ProductIcon } from "../components/ProductIcon";

const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : "—");

function useLoad<T>(fn: () => Promise<T>, initial: T) {
  const [data, setData] = useState<T>(initial);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => fn().then((d) => { setData(d); setError(null); }).catch((e: Error) => setError(e.message)), [fn]);
  useEffect(() => { reload(); }, [reload]);
  return { data, error, setError, reload };
}

const DAY = 86400000;

/** Validity as a badge: expired, expiring within 30 days, or valid. */
export function CertificateValidity({ info }: { info: CertificateInfo }) {
  const left = Math.floor((new Date(info.not_after).getTime() - Date.now()) / DAY);
  if (left < 0) return <span className="badge catalog-status breached" title={`Expired ${when(info.not_after)}`}>Expired</span>;
  if (left <= 30) return <span className="badge catalog-status unknown" title={`Valid until ${when(info.not_after)}`}>Expires in {left} {left === 1 ? "day" : "days"}</span>;
  return <span className="badge" title={`Valid until ${when(info.not_after)}`}>Valid until {new Date(info.not_after).toLocaleDateString()}</span>;
}

const readText = (file: File) => file.text();
const readBase64 = async (file: File) => {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
};

/** A PEM textarea with a "Load file…" button; the file is read locally and sent only on save. */
function PemField({ id, label, value, onChange, error, hint, accept, required = false }: {
  id: string; label: string; value: string; onChange: (v: string) => void; error?: string; hint?: string; accept: string; required?: boolean;
}) {
  return <div className="field">
    <div className="row" style={{ justifyContent: "space-between" }}>
      <label htmlFor={id}>{label}{required ? " *" : ""}</label>
      <label className="text-button" style={{ fontSize: 12 }}>Load file…
        <input type="file" accept={accept} hidden onChange={async (e) => { const f = e.target.files?.[0]; if (f) onChange(await readText(f)); e.target.value = ""; }} /></label>
    </div>
    <textarea id={id} className="short" spellCheck={false} value={value} aria-invalid={!!error} onChange={(e) => onChange(e.target.value)}
      placeholder={"-----BEGIN …-----"} />
    {error ? <span className="field-error">{error}</span> : hint && <span className="hint">{hint}</span>}
  </div>;
}

type SecretForm = {
  scope: string; owner: string; name: string; description: string; kind: "string" | "certificate";
  value: string; source: "pem" | "pkcs12"; certificate: string; private_key: string; ca_chain: string; password: string;
  pkcs12: { name: string; base64: string } | null;
};
const emptySecret: SecretForm = { scope: "user", owner: "me", name: "", description: "", kind: "string", value: "", source: "pem",
  certificate: "", private_key: "", ca_chain: "", password: "", pkcs12: null };

function SecretUsage({ secret }: { secret: Secret }) {
  const variable = secret.name.toUpperCase().replace(/[^A-Z0-9]+/g, "_");
  const cert = secret.kind === "certificate";
  return <>
    <h3>Use it in a program</h3>
    <pre className="code">{`"secrets": { "${variable}": "${secret.ref}" }`}</pre>
    {cert ? <>
      <p className="hint">The run receives the certificate as <code>{variable}</code>{secret.certificate?.has_private_key && <>, the private key as <code>{variable}_KEY</code></>}
        {!!secret.certificate?.chain_length && <> and the CA chain as <code>{variable}_CA</code></>}. The SDK writes them to private files for TLS clients:</p>
      <pre className="code">{`c = fdp.certificate("${variable}")          # Python
requests.get(url, cert=(c.cert, c.key), verify=c.ca or True)

c <- fdp::certificate("${variable}")         # R`}</pre>
    </> : <p className="hint">It arrives as the environment variable <code>{variable}</code> and is masked as <code>***</code> in run consoles.</p>}
  </>;
}

/** Secrets you can use: yours, your groups', your service accounts'. Values are write-only. */
export function SecretsTab({ hub, selectedRef, creating = false, openTab }: { hub: Hub; selectedRef?: string; creating?: boolean; openTab: OpenTab }) {
  const state = useCached("catalog:secrets", api.secrets);
  const secrets = state.data ?? [];
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const reload = state.refresh;
  const { data: accounts } = useLoad(api.serviceAccounts, [] as ServiceAccount[]);
  const [form, setForm] = useState<SecretForm>(emptySecret);
  const [formOpen, setFormOpen] = useState(creating);
  const selected = secrets.find((s) => s.ref === selectedRef);
  useEffect(() => { if (creating) { setForm(emptySecret); setFormOpen(true); } }, [creating]);
  const showForm = formOpen || (state.status === "ready" && !secrets.length);
  const replacing = secrets.find((s) => s.scope === form.scope && s.name === form.name && (s.scope === "user" || s.owner === form.owner));
  const set = (patch: Partial<SecretForm>) => setForm((f) => ({ ...f, ...patch }));
  const startReplace = (s: Secret) => {
    setForm({ ...emptySecret, scope: s.scope, owner: s.scope === "user" ? "me" : s.owner, name: s.name, description: s.description, kind: s.kind });
    setFieldErrors({}); setFormOpen(true);
    requestAnimationFrame(() => document.getElementById(s.kind === "certificate" ? "sec-cert" : "sec-value")?.focus());
  };

  const owners = form.scope === "group" ? hub.me?.groups ?? [] : form.scope === "sa" ? accounts.filter((a) => a.can_manage).map((a) => a.name) : ["me"];
  const ready = !!form.name && !!form.owner && (form.kind === "string" ? !!form.value
    : form.source === "pkcs12" ? !!form.pkcs12 : !!form.certificate.trim());
  const save = async () => {
    setBusy(true); setError(null); setFieldErrors({});
    const body: SecretInput = form.kind === "string" ? { kind: "string", value: form.value, description: form.description }
      : form.source === "pkcs12" ? { kind: "certificate", pkcs12_base64: form.pkcs12!.base64, password: form.password || undefined, description: form.description }
      : { kind: "certificate", certificate: form.certificate, private_key: form.private_key || undefined, ca_chain: form.ca_chain || undefined,
          password: form.password || undefined, description: form.description };
    try {
      const saved = await api.setSecret(form.scope, form.owner, form.name, body);
      setForm(emptySecret); setFormOpen(false);
      reload();
      openTab({ kind: "secrets", ref: saved.ref, title: saved.name });
    } catch (e) {
      setError((e as Error).message);
      if (e instanceof ApiError) setFieldErrors(e.fieldErrors);
    } finally { setBusy(false); }
  };

  return (
    <>
      <PageHeader icon={<IconKey size={22} />} title="Secrets"
        subtitle={<>API keys, passwords, tokens and certificates for your programs, dashboards, dbt and Airflow. Stored in OpenBao; values and private keys are never shown again.</>}
        actions={!formOpen && <button className="btn" onClick={() => { setForm(emptySecret); setFieldErrors({}); setFormOpen(true); }}><IconPlus size={14} />Add secret</button>} />
      {(error || state.error) && <p className="error" role="alert">{error || state.error}</p>}
      {showForm && <form className="card" onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <h3>{replacing ? `Replace ${form.name}` : "Add a secret"}</h3>
        <div className="arg-grid">
          <div className="field">
            <label htmlFor="sec-scope">Belongs to</label>
            <select id="sec-scope" value={form.scope} onChange={(e) => {
              const scope = e.target.value;
              const first = scope === "group" ? hub.me?.groups[0] ?? "" : scope === "sa" ? accounts.find((a) => a.can_manage)?.name ?? "" : "me";
              set({ scope, owner: first });
            }}>
              <option value="user">Me (private)</option>
              <option value="group">A group (shared)</option>
              <option value="sa" disabled={!accounts.length}>A service account</option>
            </select>
          </div>
          {form.scope !== "user" && (
            <div className="field">
              <label htmlFor="sec-owner">{form.scope === "group" ? "Group" : "Service account"}</label>
              <select id="sec-owner" value={form.owner} onChange={(e) => set({ owner: e.target.value })}>
                {owners.map((o) => <option key={o}>{o}</option>)}
              </select>
            </div>
          )}
          <div className="field">
            <label htmlFor="sec-name">Name</label>
            <input id="sec-name" className="mono" value={form.name} onChange={(e) => set({ name: e.target.value })} placeholder={form.kind === "certificate" ? "finance-api-client" : "openai"} />
          </div>
          <div className="field">
            <label htmlFor="sec-desc">Description</label>
            <input id="sec-desc" value={form.description} onChange={(e) => set({ description: e.target.value })} />
          </div>
        </div>
        <div className="field">
          <span className="hint" id="sec-type-label">Type</span>
          <div className="segmented" role="group" aria-labelledby="sec-type-label" style={{ alignSelf: "flex-start" }}>
            <button type="button" aria-pressed={form.kind === "string"} onClick={() => set({ kind: "string" })}>String value</button>
            <button type="button" aria-pressed={form.kind === "certificate"} onClick={() => set({ kind: "certificate" })}>Certificate</button>
          </div>
        </div>
        {form.kind === "string" ? <div className="field" style={{ maxWidth: 720 }}>
          <label htmlFor="sec-value">Value</label>
          <input id="sec-value" type="password" autoComplete="off" value={form.value} aria-invalid={!!fieldErrors.value} onChange={(e) => set({ value: e.target.value })} />
          {fieldErrors.value && <span className="field-error">{fieldErrors.value}</span>}
        </div> : <>
          <div className="field">
            <span className="hint" id="sec-source-label">Format</span>
            <div className="segmented" role="group" aria-labelledby="sec-source-label" style={{ alignSelf: "flex-start" }}>
              <button type="button" aria-pressed={form.source === "pem"} onClick={() => set({ source: "pem" })}>PEM</button>
              <button type="button" aria-pressed={form.source === "pkcs12"} onClick={() => set({ source: "pkcs12" })}>PKCS#12 (.p12 / .pfx)</button>
            </div>
          </div>
          {form.source === "pem" ? <div className="arg-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))" }}>
            <PemField id="sec-cert" label="Certificate" required value={form.certificate} onChange={(v) => set({ certificate: v })} accept=".pem,.crt,.cer"
              error={fieldErrors.certificate} hint="The certificate; intermediates may follow it." />
            <PemField id="sec-key" label="Private key" value={form.private_key} onChange={(v) => set({ private_key: v })} accept=".pem,.key"
              error={fieldErrors.private_key} hint="Optional. Must belong to the certificate." />
            <PemField id="sec-ca" label="CA chain" value={form.ca_chain} onChange={(v) => set({ ca_chain: v })} accept=".pem,.crt,.cer"
              error={fieldErrors.ca_chain} hint="Optional. Issuers to trust or present." />
          </div> : <div className="field" style={{ maxWidth: 720 }}>
            <label htmlFor="sec-p12">PKCS#12 file *</label>
            <input id="sec-p12" type="file" accept=".p12,.pfx" onChange={async (e) => {
              const f = e.target.files?.[0]; set({ pkcs12: f ? { name: f.name, base64: await readBase64(f) } : null });
            }} />
            {fieldErrors.pkcs12 ? <span className="field-error">{fieldErrors.pkcs12}</span> : <span className="hint">Certificate, private key and chain in one file.</span>}
          </div>}
          <div className="field" style={{ maxWidth: 360 }}>
            <label htmlFor="sec-password">{form.source === "pkcs12" ? "File password" : "Key password"}</label>
            <input id="sec-password" type="password" autoComplete="off" value={form.password} onChange={(e) => set({ password: e.target.value })} />
            <span className="hint">Only to open an encrypted {form.source === "pkcs12" ? "file" : "key"}; not stored.</span>
          </div>
        </>}
        <div className="form-actions">
          <button className="btn" disabled={busy || !ready}>{busy ? "Saving…" : "Save secret"}</button>
          {secrets.length > 0 && <button type="button" className="btn secondary" onClick={() => { setFormOpen(false); setForm(emptySecret); setFieldErrors({}); }}>Cancel</button>}
          {form.name && <span className="hint mono">
            use as {form.scope === "user" ? "me" : form.scope === "group" ? `group:${form.owner}` : `sa:${form.owner}`}/{form.name}</span>}
        </div>
      </form>}
      {selected && !formOpen && <section className="card" aria-label={`Secret ${selected.name}`}>
        <div className="section-heading" style={{ marginTop: 0 }}>
          <h2 className="row">{selected.kind === "certificate" ? <ProductIcon kind="identity" size={18} /> : <IconKey size={18} />}{selected.name}</h2>
          <div className="row">
            <button className="btn sm secondary" onClick={() => startReplace(selected)}>Replace</button>
            <button className="icon-btn" title="Close" aria-label="Close secret details" onClick={() => openTab({ kind: "secrets", title: "Secrets" })}><IconClose /></button>
          </div>
        </div>
        {selected.description && <p className="muted" style={{ marginTop: -4 }}>{selected.description}</p>}
        {selected.kind === "certificate" && selected.certificate ? <dl className="kv">
          <dt>Subject</dt><dd className="mono">{selected.certificate.subject}</dd>
          <dt>Issuer</dt><dd className="mono">{selected.certificate.issuer}</dd>
          <dt>Valid</dt><dd>{when(selected.certificate.not_before)} → {when(selected.certificate.not_after)} <CertificateValidity info={selected.certificate} /></dd>
          {selected.certificate.subject_alt_names.length > 0 && <><dt>Alternative names</dt><dd className="mono">{selected.certificate.subject_alt_names.join(", ")}</dd></>}
          <dt>Private key</dt><dd>{selected.certificate.has_private_key ? "Included (never shown)" : "Not included"}</dd>
          <dt>CA chain</dt><dd>{selected.certificate.chain_length ? `${selected.certificate.chain_length} certificate${selected.certificate.chain_length === 1 ? "" : "s"}` : "None"}</dd>
          <dt>SHA-256</dt><dd className="mono" style={{ fontSize: 11 }}>{selected.certificate.sha256_fingerprint}</dd>
          <dt>Serial</dt><dd className="mono">{selected.certificate.serial}</dd>
        </dl> : <dl className="kv"><dt>Type</dt><dd>String value (never shown)</dd></dl>}
        <dl className="kv" style={{ marginTop: 8 }}><dt>Updated</dt><dd>{when(selected.updated_at)} · {selected.updated_by}</dd></dl>
        <SecretUsage secret={selected} />
      </section>}
      <table aria-label="Secrets">
        <thead><tr><th>Name</th><th>Type</th><th>Belongs to</th><th>Description</th><th>Updated</th><th /></tr></thead>
        <tbody>
          {secrets.map((s) => (
            <tr key={`${s.scope}:${s.owner}/${s.name}`} className={s.ref === selectedRef ? "selected" : ""}>
              <td><button className="text-button object-name mono" onClick={() => openTab({ kind: "secrets", ref: s.ref, title: s.name })}>{s.ref}</button></td>
              <td>{s.kind === "certificate" ? <span className="row" style={{ gap: 6, flexWrap: "wrap" }}>Certificate {s.certificate && <CertificateValidity info={s.certificate} />}</span> : "String"}</td>
              <td>{s.scope === "user" ? "You" : s.scope === "group" ? `Group ${s.owner}` : `Service account ${s.owner}`}</td>
              <td>{s.description || <span className="muted">—</span>}</td>
              <td className="muted">{when(s.updated_at)} · {s.updated_by}</td>
              <td className="table-actions"><div className="row">
                <button className="btn sm secondary" onClick={() => startReplace(s)}>Replace</button>
                <button className="icon-btn" aria-label={`Delete ${s.ref}`} title="Delete secret" onClick={() => confirm(`Delete secret ${s.ref}? Programs using it will fail to start.`)
                  && api.deleteSecret(s.scope, s.scope === "user" ? "me" : s.owner, s.name).then(reload).catch((e: Error) => setError(e.message))}><IconTrash /></button>
              </div></td>
            </tr>
          ))}
          {!secrets.length && <tr><td colSpan={6}>No secrets yet.</td></tr>}
        </tbody>
      </table>
    </>
  );
}

const KINDS: RuntimeKind[] = ["python", "r", "dbt", "airflow"];
const CONFIG_KEY: Record<RuntimeKind, string> = { python: "python", r: "r", dbt: "dbt_version", airflow: "airflow" };

/** Admin: which Python, R, dbt and Airflow versions users may choose, and their images. */
export function AdminRuntimes({ withR = true }: { withR?: boolean }) {
  const { data: rows, error, setError, reload } = useLoad(api.admin.runtimes, [] as Runtime[]);
  const [form, setForm] = useState({ kind: "python" as RuntimeKind, version: "", image: "", description: "", is_default: false, enabled: true });
  const [adding, setAdding] = useState(false);

  const save = async () => {
    try {
      await api.admin.upsertRuntime(form.kind, form.version, form);
      setForm({ ...form, version: "", image: "", description: "", is_default: false });
      setAdding(false);
      reload();
    } catch (e) { setError((e as Error).message); }
  };
  const toggle = (r: Runtime, patch: Partial<Runtime>) =>
    api.admin.upsertRuntime(r.kind, r.version, { image: r.image, description: r.description, is_default: r.is_default, enabled: r.enabled, ...patch })
      .then(reload).catch((e: Error) => setError(e.message));

  return (
    <>
      <PageHeader title="Runtimes"
        subtitle={<>Versions users can pick per program in <code>fdp.json</code> (up to 5 per runtime). Programs that don't pick one get the default.
          Versions marked <b>Installation</b> come with the platform: their images are set in the Helm values and follow every upgrade.
          Offer more versions here or with OpenTofu (<code>fdp_runtime</code>), with any image from your registry.</>}
        actions={<button className="btn" aria-expanded={adding} onClick={() => setAdding(!adding)}><IconPlus size={14} />Offer a version</button>} />
      {error && <p className="error" role="alert">{error}</p>}
      {adding && <form className="card" onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <h3>Offer a version</h3>
        <div className="arg-grid">
          <div className="field"><label htmlFor="rt-kind">Runtime</label>
            <select id="rt-kind" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as RuntimeKind })}>
              {KINDS.filter((k) => k !== "r" || withR).map((k) => <option key={k} value={k}>{runtimeLabel(k)}</option>)}
            </select></div>
          <div className="field"><label htmlFor="rt-version">Version label</label>
            <input id="rt-version" className="mono" autoFocus value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} placeholder="3.11" /></div>
          <div className="field"><label htmlFor="rt-image">Image</label>
            <input id="rt-image" className="mono" value={form.image} onChange={(e) => setForm({ ...form, image: e.target.value })} placeholder="fdp/compute-python:3.11" /></div>
          <div className="field"><label htmlFor="rt-desc">Description</label>
            <input id="rt-desc" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></div>
        </div>
        <label className="row"><input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} /> Make it the default</label>
        <div className="form-actions">
          <button className="btn" disabled={!form.version || !form.image}>Save</button>
          <button type="button" className="btn secondary" onClick={() => setAdding(false)}>Cancel</button>
        </div>
      </form>}
      {KINDS.filter((k) => k !== "r" || withR).map((k) => (
        <section key={k}>
          <div className="section-heading"><h2>{runtimeLabel(k)} <span className="muted mono" style={{ fontSize: 12, fontWeight: 400 }}>"{CONFIG_KEY[k]}": "…"</span></h2></div>
          <table style={{ tableLayout: "fixed" }}>
            <colgroup><col style={{ width: "20%" }} /><col /><col style={{ width: 140 }} /><col style={{ width: 80 }} /><col style={{ width: 48 }} /></colgroup>
            <thead><tr><th>Version</th><th>Image</th><th>Default</th><th>Offered</th><th /></tr></thead>
            <tbody>
              {rows.filter((r) => r.kind === k).map((r) => (
                <tr key={r.id}>
                  <td className="mono"><b>{r.version}</b>{r.installation && <> <span className="badge" title="Shipped with the installation; image from the Helm values">Installation</span></>}<div className="muted">{r.description}</div></td>
                  <td className="mono">{r.image}</td>
                  <td>{r.is_default ? <span className="badge">Default</span> : <button className="btn sm secondary" onClick={() => toggle(r, { is_default: true })}>Make default</button>}</td>
                  <td><input type="checkbox" aria-label={`Offer ${k} ${r.version}`} checked={r.enabled} disabled={r.is_default}
                             onChange={(e) => toggle(r, { enabled: e.target.checked })} /></td>
                  <td className="table-actions">{!r.is_default && !r.installation && <button className="icon-btn" aria-label={`Remove ${k} ${r.version}`} title="Remove version"
                        onClick={() => confirm(`Remove ${runtimeLabel(k, r.version)}? Programs pinned to it stop starting.`)
                          && api.admin.deleteRuntime(k, r.version).then(reload).catch((e: Error) => setError(e.message))}><IconTrash /></button>}</td>
                </tr>
              ))}
              {!rows.some((r) => r.kind === k) && <tr><td colSpan={5}>No versions offered yet.</td></tr>}
            </tbody>
          </table>
        </section>
      ))}
    </>
  );
}

/** Admin: service accounts (non-human identities for data products) and their tokens. */
export function AdminServiceAccounts({ hub, openTab }: { hub: Hub; openTab: OpenTab }) {
  const { data: accounts, error, setError, reload } = useLoad(api.serviceAccounts, [] as ServiceAccount[]);
  const empty = { name: "", description: "", layer_id: hub.layerId };
  const [form, setForm] = useState(empty);
  const [open, setOpen] = useState(false);
  const editing = accounts.some((a) => a.name === form.name);
  const serviceFor = (a: ServiceAccount) => hub.resources.find((r) => r.config.service_account && r.object_id === a.id);

  const save = async () => {
    try {
      const existing = accounts.find((a) => a.name === form.name);
      await api.admin.upsertServiceAccount(form.name, { layer_id: form.layer_id, description: form.description, groups: existing?.groups ?? [],
        allowed_groups: existing?.allowed_groups ?? [], disabled: existing?.disabled ?? false });
      setForm(empty); setOpen(false); await reload(); await hub.refresh();
    } catch (e) { setError((e as Error).message); }
  };

  return (
    <>
      <PageHeader icon={<ProductIcon kind="identity" size={22} />} title="Service accounts"
        subtitle={<>Workloads use their built-in managed identities by default. Create a separate service account for an external client or an intentionally shared identity. Manage its tokens here and grant data access directly on schemas or APIs.</>}
        actions={<button className="btn" aria-expanded={open && !editing} onClick={() => { setForm(empty); setOpen(!(open && !editing)); }}><IconPlus size={14} />New service account</button>} />
      {error && <p className="error" role="alert">{error}</p>}
      {open && <form className="card" onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <h3>{editing ? `Edit ${form.name}` : "New service account"}</h3>
        <div className="arg-grid">
          <div className="field"><label htmlFor="sa-name">Name</label>
            <input id="sa-name" className="mono" autoFocus={!editing} disabled={editing} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="finance-etl" /></div>
          <div className="field"><label htmlFor="sa-desc">Description</label>
            <input id="sa-desc" autoFocus={editing} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></div>
        </div>
        <div className="field"><label htmlFor="sa-layer">Layer</label><select id="sa-layer" required disabled={editing} value={form.layer_id} onChange={(e) => setForm({ ...form, layer_id: e.target.value })}>{!form.layer_id && <option value="">Choose a layer</option>}{hub.layers.map((layer) => <option key={layer.id} value={layer.id}>{layer.name}</option>)}</select></div>
        <div className="form-actions">
          <button className="btn" disabled={!form.name}>Save</button>
          <button type="button" className="btn secondary" onClick={() => { setOpen(false); setForm(empty); }}>Cancel</button>
        </div>
      </form>}
      <table>
        <thead><tr><th>Name</th><th>Layer</th><th>Created</th><th /></tr></thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.id}>
              <td>{serviceFor(a) ? <button className="text-button object-name mono" onClick={() => openTab(resourceTab(serviceFor(a)!))}>{a.principal}</button> : <b className="mono">{a.principal}</b>}
                {a.disabled && <> <span className="badge">disabled</span></>}<div className="muted">{a.description}</div></td>
              <td>{a.layer_name || "—"}</td><td className="muted">{when(a.created_at)} · {a.created_by}</td>
              <td className="table-actions"><div className="row">
                <button className="btn sm secondary" disabled={!serviceFor(a)} onClick={() => openTab(resourceTab(serviceFor(a)!, "permissions"))}>Permissions</button>
                <button className="btn sm secondary" onClick={() => { setForm({ name: a.name, description: a.description, layer_id: a.layer_id || hub.layerId }); setOpen(true); }}>Edit</button>
                <button className="btn sm secondary" onClick={() => api.admin.upsertServiceAccount(a.name, { ...a, disabled: !a.disabled }).then(reload).catch((e: Error) => setError(e.message))}>{a.disabled ? "Enable" : "Disable"}</button>
                <button className="icon-btn" aria-label={`Delete ${a.name}`} title="Delete service account" onClick={() => confirm(`Delete ${a.principal}? Its tokens stop working.`)
                  && api.admin.deleteServiceAccount(a.name).then(reload).catch((e: Error) => setError(e.message))}><IconTrash /></button>
              </div></td>
            </tr>
          ))}
          {!accounts.length && <tr><td colSpan={3}>No service accounts yet.</td></tr>}
        </tbody>
      </table>
    </>
  );
}

/** Tokens of one service account. A new token is shown exactly once (or saved straight into a secret). */
export function TokensCard({ sa, onError }: { sa: string; onError: (m: string) => void }) {
  const fetchTokens = useCallback(() => api.tokens(sa), [sa]);
  const { data: tokens, reload } = useLoad(fetchTokens, [] as ServiceToken[]);
  const [form, setForm] = useState({ name: "", days: "90", secret: "" });
  const [fresh, setFresh] = useState<ServiceToken | null>(null);

  const create = async () => {
    try {
      const t = await api.createToken(sa, { name: form.name, expires_days: form.days ? Number(form.days) : undefined,
        store_as_secret: form.secret || undefined });
      setFresh(t); setForm({ name: "", days: "90", secret: "" }); reload();
    } catch (e) { onError((e as Error).message); }
  };

  return (
    <div className="card">
      <h2>Tokens</h2>
      <p className="muted">For tools outside Floren Data Platform (external schedulers, BI): <code>fdp.connect("finance-dw", token=…)</code> or <code>Authorization: Bearer dcs_…</code>.</p>
      {fresh?.token && (
        <div className="notice warn" role="status">
          <b>Copy this token now; it won't be shown again.</b>
          <pre className="code" style={{ userSelect: "all", color: "var(--fg)" }}>{fresh.token}</pre>
          {fresh.stored_as && <p className="muted">Also saved as secret <code>{fresh.stored_as}</code>.</p>}
          <div className="row">
            <button className="btn sm secondary" onClick={() => void navigator.clipboard?.writeText(fresh.token!)}>Copy</button>
            <button className="btn sm secondary" onClick={() => setFresh(null)}>Done</button>
          </div>
        </div>
      )}
      <table>
        <thead><tr><th>Name</th><th>Token</th><th>Expires</th><th>Last used</th><th>Saved as</th><th /></tr></thead>
        <tbody>{tokens.map((t) => (
          <tr key={t.id} style={t.revoked ? { opacity: 0.5 } : undefined}>
            <td>{t.name}</td><td className="mono">{t.prefix}…</td><td>{when(t.expires_at)}</td><td>{when(t.last_used_at)}</td>
            <td className="mono">{t.stored_as ?? "—"}</td>
            <td className="table-actions">{t.revoked ? <span className="badge">Revoked</span> : <button className="btn sm danger" onClick={() => confirm(`Revoke ${t.name}?`)
              && api.revokeToken(sa, t.id).then(reload).catch((e: Error) => onError(e.message))}>Revoke</button>}</td>
          </tr>))}
          {!tokens.length && <tr><td colSpan={6}>No tokens yet.</td></tr>}
        </tbody>
      </table>
      <form className="toolbar" style={{ justifyContent: "flex-start", marginTop: 16 }} onSubmit={(e) => { e.preventDefault(); void create(); }}>
        <div className="field" style={{ margin: 0 }}><label htmlFor="tok-name">Name</label><input id="tok-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="external-bi" /></div>
        <div className="field" style={{ margin: 0, width: 110 }}><label htmlFor="tok-days">Expires (days)</label><input id="tok-days" type="number" min={1} value={form.days} onChange={(e) => setForm({ ...form, days: e.target.value })} /></div>
        <div className="field" style={{ margin: 0 }}><label htmlFor="tok-secret">Save as secret (optional)</label><input id="tok-secret" className="mono" value={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.value })} placeholder="api-token" /></div>
        <button className="btn" disabled={!form.name}>Create token</button>
      </form>
    </div>
  );
}
