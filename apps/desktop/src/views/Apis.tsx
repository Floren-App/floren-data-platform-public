import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api, ApiCallRecord, ApiDefinition, ApiDirection, ApiError, ApiInput, ApiOperation, ApiValue, InboundConfig,
  defaultLayerId, OperationInput, usablePools, OutboundConfig, Layer, Secret, tryExternal,
} from "../api";
import type { Hub } from "../hub";
import type { TabRef } from "./Tabs";
import { PageHeader } from "../components/Page";
import { PermissionsPanel } from "../components/Permissions";
import { ProductIcon } from "../components/ProductIcon";
import { IconApi, IconPlay, IconPlus, IconTrash } from "../components/Icons";
import { useCached } from "../cache";

type OpenTab = (t: TabRef) => void;
type Errors = Record<string, string>;

const DIRECTION_TEXT: Record<ApiDirection, { title: string; text: string }> = {
  internal: { title: "Internal", text: "Endpoints that outside systems call to run deployed programs inside Floren Data Platform. Callers prove who they are with JWT and/or mTLS; programs run as the API's own managed identity. Never direct data access." },
  external: { title: "External", text: "Outside REST and SOAP services that programs call through Floren Data Platform, as their own identity. Credentials, headers and client certificates come from secrets and never reach the program." },
};

const defaultInbound = (): InboundConfig => ({
  jwt: { enabled: true, issuer: "", audience: "", jwks_uri: "", required_claims: {}, algorithms: ["RS256", "ES256", "PS256"] },
  mtls: { enabled: false, ca_secret: "", allowed_subjects: [], allowed_thumbprints: [] },
  soap_version: "1.1",
  anonymous: false,
});

/** Red warning for internal APIs without caller authentication. */
const AnonymousWarning = () => <p className="danger-note" role="alert"><strong>No caller authentication.</strong> Anyone who can reach the
  API gateway can run these programs as the API's identity. Use this for testing only; it is refused in production layers.</p>;
const defaultOutbound = (): OutboundConfig => ({
  base_url: "", soap_version: "1.1", headers: [], query: [],
  auth: { type: "none", name: "X-API-Key", location: "header", username: "", secret: "", token_url: "", client_id: "", scope: "" },
  client_certificate: "", server_ca: "", verify_tls: true, timeout_seconds: 30, pass_request_headers: [], pass_response_headers: ["content-type"],
});

const lines = (text: string) => text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
const methodClass = (m: string) => `method method-${m.toLowerCase()}`;

function useApis() {
  const [data, setData] = useState<{ apis: ApiDefinition[]; can_create: boolean } | null>(null);
  const [error, setError] = useState("");
  const reload = useCallback(() => api.apis.list().then((d) => { setData(d); setError(""); }).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void reload(); }, [reload]);
  return { data, error, reload };
}

function FieldError({ errors, name, hint }: { errors: Errors; name: string; hint?: string }) {
  return errors[name] ? <span className="field-error">{errors[name]}</span> : hint ? <span className="hint">{hint}</span> : null;
}

/** Picks a shared secret (group or service account). Personal secrets cannot back shared configuration. */
function SecretSelect({ id, value, onChange, kind = "any", needsKey = false, placeholder = "Choose a secret" }: {
  id: string; value: string; onChange: (v: string) => void; kind?: "any" | "string" | "certificate"; needsKey?: boolean; placeholder?: string;
}) {
  const secrets = useCached("catalog:secrets", api.secrets);
  const options = (secrets.data ?? []).filter((s: Secret) => !s.ref.startsWith("me/")
    && (kind === "any" || s.kind === kind) && (!needsKey || s.certificate?.has_private_key));
  return <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
    <option value="">{placeholder}</option>
    {value && !options.some((s) => s.ref === value) && <option value={value}>{value}</option>}
    {options.map((s) => <option key={s.ref} value={s.ref}>{s.ref}{s.kind === "certificate" ? " (certificate)" : ""}</option>)}
  </select>;
}

/** Name/value rows where a value is either literal or taken from a secret. */
function ValuesEditor({ label, values, onChange, errors, prefix, namePlaceholder }: {
  label: string; values: ApiValue[]; onChange: (v: ApiValue[]) => void; errors: Errors; prefix: string; namePlaceholder: string;
}) {
  const set = (i: number, patch: Partial<ApiValue>) => onChange(values.map((v, j) => j === i ? { ...v, ...patch } : v));
  return <div className="field">
    <span className="hint" style={{ color: "var(--fg-muted)" }}>{label}</span>
    {values.length > 0 && <table className="values-table"><tbody>{values.map((v, i) => <tr key={i}>
      <td style={{ width: "30%" }}><input aria-label={`${label} ${i + 1} name`} className="mono" value={v.name} placeholder={namePlaceholder} onChange={(e) => set(i, { name: e.target.value })} />
        <FieldError errors={errors} name={`${prefix}.${i}.name`} /></td>
      <td style={{ width: 110 }}><select aria-label={`${label} ${i + 1} source`} value={v.secret ? "secret" : "value"} onChange={(e) => set(i, e.target.value === "secret" ? { value: "", secret: " " } : { secret: "" })}>
        <option value="value">Value</option><option value="secret">Secret</option></select></td>
      <td>{v.secret ? <SecretSelect id={`${prefix}-${i}-secret`} kind="string" value={v.secret.trim()} onChange={(s) => set(i, { secret: s || " " })} />
        : <input aria-label={`${label} ${i + 1} value`} value={v.value} onChange={(e) => set(i, { value: e.target.value })} />}
        <FieldError errors={errors} name={`${prefix}.${i}.secret`} /></td>
      <td className="table-actions"><button type="button" className="icon-btn" aria-label={`Remove ${label} ${i + 1}`} onClick={() => onChange(values.filter((_, j) => j !== i))}><IconTrash /></button></td>
    </tr>)}</tbody></table>}
    <div><button type="button" className="btn sm secondary" onClick={() => onChange([...values, { name: "", value: "", secret: "" }])}><IconPlus size={14} />Add {label.toLowerCase().replace(/s$/, "")}</button></div>
  </div>;
}

const cleanValues = (values: ApiValue[]) => values.filter((v) => v.name.trim()).map((v) => ({ ...v, secret: v.secret.trim() }));

/** The API's own settings: caller authentication (internal) or the backend (external). Used to create and edit. */
function ApiSettingsForm({ initial, creating, onSave, onCancel, layers, layerId }: {
  initial: ApiInput; creating: boolean; layers: Layer[]; layerId: string; onSave: (body: ApiInput) => Promise<void>; onCancel?: () => void;
}) {
  const [form, setForm] = useState<ApiInput>({ ...initial, layer_id: initial.layer_id || layerId || defaultLayerId(layers) });
  const [errors, setErrors] = useState<Errors>({});
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [claimsText, setClaimsText] = useState(Object.entries(initial.inbound?.jwt.required_claims ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"));
  // List fields keep what is typed (commas, new lines) and are parsed on save.
  const [text, setText] = useState({
    subjects: (initial.inbound?.mtls.allowed_subjects ?? []).join("\n"), thumbprints: (initial.inbound?.mtls.allowed_thumbprints ?? []).join("\n"),
    passRequest: (initial.outbound?.pass_request_headers ?? []).join(", "), passResponse: (initial.outbound?.pass_response_headers ?? ["content-type"]).join(", "),
  });
  const inbound = form.inbound ?? defaultInbound(), outbound = form.outbound ?? defaultOutbound();
  const setIn = (patch: Partial<InboundConfig>) => setForm({ ...form, inbound: { ...inbound, ...patch } });
  const setOut = (patch: Partial<OutboundConfig>) => setForm({ ...form, outbound: { ...outbound, ...patch } });
  const internal = form.direction === "internal";

  const save = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setErrors({}); setMessage("");
    const body: ApiInput = internal
      ? { ...form, inbound: { ...inbound, jwt: { ...inbound.jwt, required_claims: Object.fromEntries(claimsText.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => [l.split("=")[0].trim(), l.split("=").slice(1).join("=").trim()])) },
          mtls: { ...inbound.mtls, allowed_subjects: text.subjects.split("\n").map((l) => l.trim()).filter(Boolean), allowed_thumbprints: lines(text.thumbprints) } } }
      : { ...form, outbound: { ...outbound, headers: cleanValues(outbound.headers), query: cleanValues(outbound.query),
          pass_request_headers: lines(text.passRequest), pass_response_headers: lines(text.passResponse) } };
    try { await onSave(body); if (!creating) setMessage("Settings saved."); }
    catch (err) { setMessage(""); if (err instanceof ApiError) setErrors({ ...err.fieldErrors, _: err.message }); else setErrors({ _: (err as Error).message }); }
    finally { setBusy(false); }
  };

  return <form className="card" onSubmit={save} aria-label={creating ? "Create API" : "API settings"}>
    {creating && <h3>New {internal ? "internal" : "external"} API</h3>}
    {errors._ && <p className="error" role="alert">{errors._}</p>}
    {message && <p className="success-message" role="status">{message}</p>}
    <div className="field"><label htmlFor="api-layer">Layer</label><select id="api-layer" required disabled={!creating} value={form.layer_id} onChange={(e) => setForm({ ...form, layer_id: e.target.value })}>{!form.layer_id && <option value="">Choose a layer</option>}{layers.map((layer) => <option key={layer.id} value={layer.id}>{layer.name}</option>)}</select></div>
    <div className="arg-grid">
      <div className="field"><label htmlFor="api-name">Name *</label>
        <input id="api-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={internal ? "Orders" : "CRM"} />
        <FieldError errors={errors} name="name" /></div>
      <div className="field"><label htmlFor="api-slug">Address *</label>
        <input id="api-slug" className="mono" value={form.slug} disabled={!creating} placeholder="orders"
          onChange={(e) => setForm({ ...form, slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") })} />
        <FieldError errors={errors} name="slug" hint={internal ? `Callers use …/gw/${form.slug || "<address>"}` : `Programs call fdp.api("${form.slug || "<address>"}")`} /></div>
      <div className="field"><label htmlFor="api-protocol">Protocol</label>
        <select id="api-protocol" value={form.protocol} onChange={(e) => setForm({ ...form, protocol: e.target.value as "rest" | "soap" })}>
          <option value="rest">REST (JSON)</option><option value="soap">SOAP (XML)</option></select></div>
      <div className="field"><label htmlFor="api-description">Description</label>
        <input id="api-description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></div>
    </div>

    {internal ? <>
      <h3>Caller authentication</h3>
      <p className="hint" style={{ marginTop: -4 }}>Every call must pass each enabled check. At least one is required, unless anonymous callers are allowed for testing.</p>
      <FieldError errors={errors} name="inbound.auth" />
      <label className="row"><input type="checkbox" checked={!!inbound.anonymous}
        onChange={(e) => setIn(e.target.checked ? { anonymous: true, jwt: { ...inbound.jwt, enabled: false }, mtls: { ...inbound.mtls, enabled: false } } : { anonymous: false })} />Allow anonymous callers (testing only)</label>
      {inbound.anonymous && <AnonymousWarning />}
      <fieldset className="plain" disabled={!!inbound.anonymous}>
      <label className="row"><input type="checkbox" checked={inbound.jwt.enabled} onChange={(e) => setIn({ jwt: { ...inbound.jwt, enabled: e.target.checked } })} />Require a JWT (OAuth 2.0 / OpenID Connect bearer token)</label>
      {inbound.jwt.enabled && <div className="arg-grid nested">
        <div className="field"><label htmlFor="jwt-issuer">Issuer *</label><input id="jwt-issuer" className="mono" value={inbound.jwt.issuer} placeholder="https://login.partner.example" onChange={(e) => setIn({ jwt: { ...inbound.jwt, issuer: e.target.value } })} /><FieldError errors={errors} name="inbound.jwt.issuer" /></div>
        <div className="field"><label htmlFor="jwt-audience">Audience *</label><input id="jwt-audience" className="mono" value={inbound.jwt.audience} placeholder="api://orders" onChange={(e) => setIn({ jwt: { ...inbound.jwt, audience: e.target.value } })} /><FieldError errors={errors} name="inbound.jwt.audience" /></div>
        <div className="field"><label htmlFor="jwt-jwks">Signing keys (JWKS URL) *</label><input id="jwt-jwks" className="mono" value={inbound.jwt.jwks_uri} placeholder="https://login.partner.example/.well-known/jwks.json" onChange={(e) => setIn({ jwt: { ...inbound.jwt, jwks_uri: e.target.value } })} /><FieldError errors={errors} name="inbound.jwt.jwks_uri" /></div>
        <div className="field"><label htmlFor="jwt-claims">Required claims</label><textarea id="jwt-claims" className="short" value={claimsText} placeholder={"scope=orders.run\nroles=OrdersCaller"} onChange={(e) => setClaimsText(e.target.value)} /><span className="hint">One claim=value per line; list or space-separated claims must contain the value.</span></div>
      </div>}
      <label className="row"><input type="checkbox" checked={inbound.mtls.enabled} onChange={(e) => setIn({ mtls: { ...inbound.mtls, enabled: e.target.checked } })} />Require a client certificate (mTLS)</label>
      {inbound.mtls.enabled && <div className="arg-grid nested">
        <div className="field"><label htmlFor="mtls-ca">Trusted CA *</label><SecretSelect id="mtls-ca" kind="certificate" value={inbound.mtls.ca_secret} onChange={(v) => setIn({ mtls: { ...inbound.mtls, ca_secret: v } })} placeholder="Choose a certificate secret" />
          <FieldError errors={errors} name="inbound.mtls.ca_secret" hint="Client certificates must be issued directly by this CA (or its chain)." /></div>
        <div className="field"><label htmlFor="mtls-subjects">Allowed subjects</label><textarea id="mtls-subjects" className="short" value={text.subjects} placeholder="CN=partner-client,O=Partner" onChange={(e) => setText({ ...text, subjects: e.target.value })} /><span className="hint">Optional. One per line; empty allows any certificate from the CA.</span></div>
        <div className="field"><label htmlFor="mtls-thumbprints">Allowed thumbprints (SHA-256)</label><textarea id="mtls-thumbprints" className="short mono" value={text.thumbprints} onChange={(e) => setText({ ...text, thumbprints: e.target.value })} /><span className="hint">Optional pinning.</span></div>
      </div>}
      </fieldset>
      {form.protocol === "soap" && <div className="field" style={{ maxWidth: 260 }}><label htmlFor="soap-version">SOAP version</label>
        <select id="soap-version" value={inbound.soap_version} onChange={(e) => setIn({ soap_version: e.target.value as "1.1" | "1.2" })}><option value="1.1">1.1</option><option value="1.2">1.2</option></select></div>}
    </> : <>
      <h3>Backend</h3>
      <div className="arg-grid">
        <div className="field" style={{ gridColumn: "span 2" }}><label htmlFor="out-url">Base URL *</label><input id="out-url" className="mono" value={outbound.base_url} placeholder="https://api.crm.example/v2" onChange={(e) => setOut({ base_url: e.target.value })} /><FieldError errors={errors} name="outbound.base_url" /></div>
        <div className="field"><label htmlFor="out-timeout">Timeout (s)</label><input id="out-timeout" type="number" min={1} max={120} value={outbound.timeout_seconds} onChange={(e) => setOut({ timeout_seconds: Number(e.target.value) })} /></div>
        {form.protocol === "soap" && <div className="field"><label htmlFor="out-soap">SOAP version</label><select id="out-soap" value={outbound.soap_version} onChange={(e) => setOut({ soap_version: e.target.value as "1.1" | "1.2" })}><option value="1.1">1.1</option><option value="1.2">1.2</option></select></div>}
      </div>
      <h3>Authentication</h3>
      <div className="arg-grid">
        <div className="field"><label htmlFor="out-auth">Type</label><select id="out-auth" value={outbound.auth.type} onChange={(e) => setOut({ auth: { ...outbound.auth, type: e.target.value as OutboundConfig["auth"]["type"] } })}>
          <option value="none">None</option><option value="api_key">API key</option><option value="basic">Basic</option><option value="bearer">Bearer token</option><option value="oauth2">OAuth 2.0 client credentials</option></select></div>
        {outbound.auth.type === "api_key" && <>
          <div className="field"><label htmlFor="out-key-name">Name</label><input id="out-key-name" className="mono" value={outbound.auth.name} onChange={(e) => setOut({ auth: { ...outbound.auth, name: e.target.value } })} /><FieldError errors={errors} name="outbound.auth.name" /></div>
          <div className="field"><label htmlFor="out-key-loc">Sent in</label><select id="out-key-loc" value={outbound.auth.location} onChange={(e) => setOut({ auth: { ...outbound.auth, location: e.target.value as "header" | "query" } })}><option value="header">Header</option><option value="query">Query string</option></select></div>
        </>}
        {outbound.auth.type === "basic" && <div className="field"><label htmlFor="out-user">Username</label><input id="out-user" value={outbound.auth.username} onChange={(e) => setOut({ auth: { ...outbound.auth, username: e.target.value } })} /><FieldError errors={errors} name="outbound.auth.username" /></div>}
        {outbound.auth.type === "oauth2" && <>
          <div className="field"><label htmlFor="out-token-url">Token URL</label><input id="out-token-url" className="mono" value={outbound.auth.token_url} onChange={(e) => setOut({ auth: { ...outbound.auth, token_url: e.target.value } })} /><FieldError errors={errors} name="outbound.auth.token_url" /></div>
          <div className="field"><label htmlFor="out-client-id">Client ID</label><input id="out-client-id" className="mono" value={outbound.auth.client_id} onChange={(e) => setOut({ auth: { ...outbound.auth, client_id: e.target.value } })} /><FieldError errors={errors} name="outbound.auth.client_id" /></div>
          <div className="field"><label htmlFor="out-scope">Scope</label><input id="out-scope" className="mono" value={outbound.auth.scope} onChange={(e) => setOut({ auth: { ...outbound.auth, scope: e.target.value } })} /></div>
        </>}
        {outbound.auth.type !== "none" && <div className="field"><label htmlFor="out-secret">{({ api_key: "API key", basic: "Password", bearer: "Token", oauth2: "Client secret" } as Record<string, string>)[outbound.auth.type]} secret</label>
          <SecretSelect id="out-secret" kind="string" value={outbound.auth.secret} onChange={(v) => setOut({ auth: { ...outbound.auth, secret: v } })} /><FieldError errors={errors} name="outbound.auth.secret" /></div>}
      </div>
      <h3>Headers and query</h3>
      <ValuesEditor label="Headers" prefix="outbound.headers" namePlaceholder="X-Tenant" values={outbound.headers} errors={errors} onChange={(headers) => setOut({ headers })} />
      <ValuesEditor label="Query parameters" prefix="outbound.query" namePlaceholder="api-version" values={outbound.query} errors={errors} onChange={(query) => setOut({ query })} />
      <h3>TLS</h3>
      <div className="arg-grid">
        <div className="field"><label htmlFor="out-client-cert">Client certificate (mTLS)</label><SecretSelect id="out-client-cert" kind="certificate" needsKey value={outbound.client_certificate} onChange={(v) => setOut({ client_certificate: v })} placeholder="None" />
          <FieldError errors={errors} name="outbound.client_certificate" hint="Presented to the backend; needs a private key." /></div>
        <div className="field"><label htmlFor="out-server-ca">Trusted server CA</label><SecretSelect id="out-server-ca" kind="certificate" value={outbound.server_ca} onChange={(v) => setOut({ server_ca: v })} placeholder="Public CAs" />
          <FieldError errors={errors} name="outbound.server_ca" hint="For backends with a private CA." /></div>
      </div>
      {!outbound.server_ca && <label className="row"><input type="checkbox" checked={!outbound.verify_tls} onChange={(e) => setOut({ verify_tls: !e.target.checked })} />Skip certificate verification (test backends only)</label>}
      <h3>Pass-through</h3>
      <div className="arg-grid">
        <div className="field"><label htmlFor="out-pass-req">Request headers from programs</label><input id="out-pass-req" className="mono" value={text.passRequest} placeholder="X-Correlation-Id, Accept-Language" onChange={(e) => setText({ ...text, passRequest: e.target.value })} />
          <FieldError errors={errors} name="outbound.pass_request_headers.0" hint="Only these reach the backend (plus Content-Type and Accept). Authorization and cookies never pass." /></div>
        <div className="field"><label htmlFor="out-pass-resp">Response headers to programs</label><input id="out-pass-resp" className="mono" value={text.passResponse} onChange={(e) => setText({ ...text, passResponse: e.target.value })} />
          <span className="hint">Content-Type always passes.</span></div>
      </div>
    </>}
    <div className="form-actions">
      <button className="btn" disabled={busy || !form.name.trim() || !form.slug.trim()}>{busy ? "Saving…" : creating ? "Create API" : "Save settings"}</button>
      {onCancel && <button type="button" className="btn secondary" onClick={onCancel}>Cancel</button>}
    </div>
  </form>;
}

/** API management: internal and external APIs. */
export function ApisView({ direction = "internal", hub, openTab }: { direction?: ApiDirection; hub: Hub; openTab: OpenTab }) {
  const { data, error, reload } = useApis();
  const [creating, setCreating] = useState(false);
  useEffect(() => setCreating(false), [direction]);
  const list = (data?.apis ?? []).filter((a) => a.direction === direction);
  const count = (d: ApiDirection) => (data?.apis ?? []).filter((a) => a.direction === d).length;
  return <>
    <PageHeader icon={<IconApi size={22} />} title="API management" subtitle={DIRECTION_TEXT[direction].text}
      actions={data?.can_create && !creating && <button className="btn" onClick={() => setCreating(true)}><IconPlus size={14} />Create {direction} API</button>} />
    <nav className="object-tabs" aria-label="API directions">
      {(["internal", "external"] as const).map((d) => <button key={d} className={d === direction ? "active" : ""} aria-current={d === direction ? "page" : undefined}
        onClick={() => openTab({ kind: "apis", ref: d, title: "API management" })}>{DIRECTION_TEXT[d].title} <span className="badge">{count(d)}</span></button>)}
    </nav>
    {error && <p className="error" role="alert">{error}</p>}
    {creating && <ApiSettingsForm layers={hub.layers} layerId={hub.layerId} creating onCancel={() => setCreating(false)}
      initial={{ name: "", slug: "", direction, protocol: "rest", description: "", inbound: direction === "internal" ? defaultInbound() : undefined, outbound: direction === "external" ? defaultOutbound() : undefined }}
      onSave={async (body) => { const created = await api.apis.create(body); setCreating(false); await reload(); await hub.refresh(); openTab({ kind: "api", ref: created.id, title: created.name }); }} />}
    <table aria-label={`${DIRECTION_TEXT[direction].title} APIs`}>
      <thead><tr><th>API</th><th>Protocol</th><th>{direction === "internal" ? "Caller authentication" : "Backend"}</th><th>Operations</th><th>Address</th></tr></thead>
      <tbody>
        {list.map((a) => <tr key={a.id}>
          <td><button className="text-button object-name" onClick={() => openTab({ kind: "api", ref: a.id, title: a.name })}><IconApi size={16} />{a.name}</button>
            {a.description && <div className="muted">{a.description}</div>}</td>
          <td><span className="badge">{a.protocol.toUpperCase()}</span></td>
          <td>{direction === "internal" ? (a.config.inbound?.anonymous ? <span className="error">None · anyone can call (testing)</span>
              : [a.config.inbound?.jwt.enabled && "JWT", a.config.inbound?.mtls.enabled && "mTLS"].filter(Boolean).join(" + ") || "—")
            : <span className="mono muted">{a.config.outbound?.base_url}</span>}</td>
          <td>{a.operation_count}</td>
          <td className="mono muted" style={{ overflowWrap: "anywhere" }}>{direction === "internal" ? a.endpoint : `fdp.api("${a.slug}")`}</td>
        </tr>)}
        {data && !list.length && <tr><td colSpan={5}>{direction === "internal" ? "No internal APIs yet. Create one to let outside systems run deployed programs." : "No external APIs yet. Register one so programs can call it with managed credentials."}</td></tr>}
        {!data && !error && <tr><td colSpan={5}>Loading…</td></tr>}
      </tbody>
    </table>
  </>;
}

const toInput = (a: ApiDefinition): ApiInput => ({ layer_id: a.config.layer_id, environment_id: a.config.environment_id, name: a.name, slug: a.slug, direction: a.direction, protocol: a.protocol, description: a.description,
  inbound: a.direction === "internal" ? { ...defaultInbound(), ...a.config.inbound } : undefined,
  outbound: a.direction === "external" ? { ...defaultOutbound(), ...a.config.outbound } : undefined });

/** One API: operations by endpoint group, settings, calls and permissions. */
export function ApiView({ tab, hub, openTab }: { tab: TabRef; hub: Hub; openTab: OpenTab }) {
  const [item, setItem] = useState<ApiDefinition | null>(null);
  const [error, setError] = useState("");
  const reload = useCallback(() => api.apis.get(tab.ref!).then((a) => { setItem(a); setError(""); }).catch((e: Error) => setError(e.message)), [tab.ref]);
  useEffect(() => { void reload(); }, [reload]);
  if (!item) return <p className={error ? "error" : "muted"} role={error ? "alert" : undefined}>{error || "Loading API…"}</p>;
  const section = tab.apiSection ?? "operations";
  type Section = NonNullable<TabRef["apiSection"]>;
  const sections: [Section, string][] = [["operations", "Operations"], ["settings", "Settings"], ...(item.direction === "internal" ? [["calls", "Calls"] as [Section, string]] : []), ["permissions", "Permissions"]];
  const resource = hub.resources.find((r) => r.id === item.resource_id);
  const refresh = async () => { await reload(); await hub.refresh(); };
  return <>
    <nav className="breadcrumbs" aria-label="API location">
      <button className="text-button" onClick={() => openTab({ kind: "apis", ref: item.direction, title: "API management" })}>API management</button>
      <span className="breadcrumb-divider" aria-hidden="true">/</span>
      <button className="text-button" onClick={() => openTab({ kind: "apis", ref: item.direction, title: "API management" })}>{DIRECTION_TEXT[item.direction].title}</button>
      <span className="breadcrumb-divider" aria-hidden="true">/</span><span aria-current="page">{item.name}</span>
    </nav>
    <PageHeader icon={<IconApi size={22} />} title={item.name}
      subtitle={<><span className="badge">{item.protocol.toUpperCase()}</span> {item.direction} · <span className="mono">{item.direction === "internal" ? item.endpoint : `fdp.api("${item.slug}")`}</span>{item.description && ` · ${item.description}`}</>}
      actions={item.actions.includes("delete") && <button className="btn ghost danger" onClick={() => confirm(`Delete ${item.name} and its ${item.operation_count} operations?${item.direction === "internal" ? " Callers can no longer reach it and its managed identity is disabled." : ""}`)
        && api.apis.remove(item.id).then(async () => { await hub.refresh(); openTab({ kind: "apis", ref: item.direction, title: "API management" }); }).catch((e: Error) => setError(e.message))}><IconTrash size={14} />Delete API</button>} />
    {resource?.layer_name && <p className="hint">Layer · <strong>{resource.layer_name}</strong></p>}
    {item.direction === "internal" && item.config.inbound?.anonymous && <AnonymousWarning />}
    <nav className="object-tabs" aria-label="API sections">
      {sections.map(([id, label]) => <button key={id} className={section === id ? "active" : ""} aria-current={section === id ? "page" : undefined} onClick={() => openTab({ ...tab, apiSection: id, apiFocus: undefined })}>{label}</button>)}
    </nav>
    {error && <p className="error" role="alert">{error}</p>}
    {section === "operations" && <OperationsPanel item={item} hub={hub} openTab={openTab} focus={tab.apiFocus} onChanged={refresh} />}
    {section === "settings" && <>
      {item.identity && <section className="card" aria-label="Managed identity">
        <h2 className="row"><ProductIcon kind="identity" size={18} />Managed identity</h2>
        <dl className="kv"><dt>Identity</dt><dd>{item.identity.name}</dd><dt>Principal</dt><dd className="mono">{item.identity.username}</dd></dl>
        <p className="hint">Programs behind this API run as this identity, never as the caller. Assign it Executor directly on the workloads it runs
          (and data roles if they run in caller mode), or use app-group grants.</p>
        <div className="form-actions"><button className="btn secondary" onClick={() => openTab({ kind: "access", title: "App groups" })}>Bind to an app group</button></div>
      </section>}
      {item.actions.includes("edit") ? <ApiSettingsForm layers={hub.layers} layerId={hub.layerId} key={item.id} creating={false} initial={toInput(item)} onSave={async (body) => { setItem(await api.apis.update(item.id, body)); await hub.refresh(); }} />
        : <p className="notice">You can view this API; changing its settings requires the Contributor role.</p>}
    </>}
    {section === "calls" && <CallsPanel item={item} />}
    {section === "permissions" && (resource ? <PermissionsPanel resource={resource} resources={hub.resources} openTab={openTab} onChanged={hub.refresh} /> : <p className="muted">Loading permissions…</p>)}
  </>;
}

const emptyOperation = (item: ApiDefinition): OperationInput => ({
  name: "", group: "", method: item.protocol === "soap" || item.direction === "internal" ? "POST" : "GET", path: "/", soap_action: "", description: "",
  internal: { workload_id: "", mode: "async", timeout_seconds: 30, args: {}, pool: "" }, external: { headers: [], timeout_seconds: null },
});
const opInput = (op: ApiOperation): OperationInput => ({
  name: op.name, group: op.group, method: op.method, path: op.path, soap_action: op.soap_action, description: op.description,
  internal: { workload_id: op.config.workload_id ?? "", mode: op.config.mode ?? "async", timeout_seconds: op.config.timeout_seconds ?? 30, args: op.config.args ?? {}, pool: op.config.pool ?? "" },
  external: { headers: op.config.headers ?? [], timeout_seconds: op.config.timeout_seconds ?? null },
});

function OperationsPanel({ item, hub, openTab, focus, onChanged }: { item: ApiDefinition; hub: Hub; openTab: OpenTab; focus?: string; onChanged: () => Promise<void> }) {
  const ops = item.operations ?? [];
  const initial = ops.find((o) => o.resource_id === focus)?.id ?? (item.groups?.find((g) => g.resource_id === focus) ? `group:${focus}` : ops[0]?.id ?? "new");
  const [selected, setSelected] = useState<string>(initial);
  useEffect(() => { if (focus) setSelected(initial); }, [focus]); // eslint-disable-line react-hooks/exhaustive-deps
  const grouped = useMemo(() => {
    const out = new Map<string, ApiOperation[]>();
    for (const op of ops) out.set(op.group, [...(out.get(op.group) ?? []), op]);
    return [...out.entries()].sort(([a], [b]) => (a === "" ? -1 : b === "" ? 1 : a.localeCompare(b)));
  }, [ops]);
  const canCreate = item.actions.includes("create") || (item.groups ?? []).some((g) => g.actions.includes("create"));
  const op = ops.find((o) => o.id === selected);
  const group = selected.startsWith("group:") ? item.groups?.find((g) => `group:${g.resource_id}` === selected) : undefined;
  const groupResource = group && hub.resources.find((r) => r.id === group.resource_id);
  return <div className="master-detail operations">
    <aside className="group-list" aria-label="Operations">
      {canCreate && <button className="btn sm secondary" style={{ width: "100%" }} onClick={() => setSelected("new")}><IconPlus size={14} />Add operation</button>}
      <ul>
        {grouped.map(([name, items]) => <li key={name || "_"} className="op-group">
          {name && <button className={`op-group-title ${selected === `group:${item.groups?.find((g) => g.name === name)?.resource_id}` ? "active" : ""}`}
            onClick={() => setSelected(`group:${item.groups?.find((g) => g.name === name)?.resource_id}`)} title="Endpoint group: permissions apply to all its operations">{name}</button>}
          <ul>{items.map((o) => <li key={o.id}><button className={selected === o.id ? "active" : ""} onClick={() => setSelected(o.id)}>
            <span className="row" style={{ gap: 6 }}><span className={methodClass(o.method)}>{item.protocol === "soap" ? "SOAP" : o.method}</span><strong>{o.name}</strong></span>
            <span className="muted mono">{item.protocol === "soap" ? o.soap_action || o.name : o.path}</span></button></li>)}</ul>
        </li>)}
      </ul>
      {!ops.length && <p className="muted" style={{ padding: "0 4px" }}>No operations yet.</p>}
    </aside>
    <section className="group-detail" aria-label="Operation details">
      {selected === "new" && canCreate ? <OperationEditor key="new" item={item} hub={hub} initial={emptyOperation(item)} onSaved={async (saved) => { await onChanged(); setSelected(saved.id); }} />
        : op ? <>
          <OperationEditor key={op.id + op.name + op.path} item={item} hub={hub} op={op} initial={opInput(op)} onSaved={onChanged} onDeleted={async () => { await onChanged(); setSelected("new"); }} />
          {item.direction === "external" ? <TryIt item={item} op={op} /> : <CallIt item={item} op={op} hub={hub} />}
          <details className="card"><summary>Permissions for {op.name}</summary>
            {hub.resources.find((r) => r.id === op.resource_id) ? <PermissionsPanel resource={hub.resources.find((r) => r.id === op.resource_id)!} resources={hub.resources} openTab={openTab} onChanged={hub.refresh} /> : <p className="muted">Loading…</p>}
          </details>
        </>
        : group ? <>
          <h2>{group.name}</h2>
          <p className="muted">An endpoint group. Roles granted here apply to all of its operations; add an operation to the group by entering its name as the operation's group.</p>
          {groupResource && <PermissionsPanel resource={groupResource} resources={hub.resources} openTab={openTab} onChanged={hub.refresh} />}
        </>
        : <div className="empty-state"><h2>Choose an operation</h2><p className="muted">{canCreate ? "Or add one." : "You can view the operations you have access to."}</p></div>}
    </section>
  </div>;
}

function OperationEditor({ item, hub, op, initial, onSaved, onDeleted }: {
  item: ApiDefinition; hub: Hub; op?: ApiOperation; initial: OperationInput; onSaved: (op: ApiOperation) => Promise<void>; onDeleted?: () => Promise<void>;
}) {
  const [form, setForm] = useState<OperationInput>(initial);
  const [errors, setErrors] = useState<Errors>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const editable = !op || op.actions.includes("edit");
  const internal = item.direction === "internal", soap = item.protocol === "soap";
  const programs = hub.workloads.filter((p) => ["script", "notebook", "dbt"].includes(p.kind));
  const program = programs.find((p) => p.id === form.internal?.workload_id);
  const groups = [...new Set((item.operations ?? []).map((o) => o.group).filter(Boolean))];
  const setInternal = (patch: Partial<NonNullable<OperationInput["internal"]>>) => setForm({ ...form, internal: { ...form.internal!, ...patch } });
  const save = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setErrors({}); setMessage("");
    const body = { ...form, path: soap ? "/" : form.path, internal: internal ? form.internal : undefined,
      external: !internal ? { ...form.external!, headers: cleanValues(form.external!.headers) } : undefined };
    try {
      const saved = op ? await api.apis.updateOperation(item.id, op.id, body) : await api.apis.createOperation(item.id, body);
      if (op) setMessage("Operation saved.");
      await onSaved(saved);
    } catch (err) { if (err instanceof ApiError) setErrors({ ...err.fieldErrors, _: err.message }); else setErrors({ _: (err as Error).message }); }
    finally { setBusy(false); }
  };
  return <form onSubmit={save} aria-label={op ? `Operation ${op.name}` : "New operation"}>
    <div className="section-heading" style={{ marginTop: 0 }}><h2>{op ? op.name : "New operation"}</h2>
      {op && onDeleted && op.actions.includes("delete") && <button type="button" className="btn sm ghost danger" onClick={() => confirm(`Delete operation ${op.name}?`)
        && api.apis.removeOperation(item.id, op.id).then(onDeleted).catch((e: Error) => setErrors({ _: e.message }))}><IconTrash size={14} />Delete</button>}</div>
    {errors._ && <p className="error" role="alert">{errors._}</p>}
    {message && <p className="success-message" role="status">{message}</p>}
    <fieldset disabled={!editable} className="plain">
      <div className="arg-grid">
        <div className="field"><label htmlFor="op-name">Name *</label><input id="op-name" className="mono" value={form.name} placeholder={soap ? "GetCustomer" : "get-customer"} onChange={(e) => setForm({ ...form, name: e.target.value })} /><FieldError errors={errors} name="name" /></div>
        <div className="field"><label htmlFor="op-group">Endpoint group</label><input id="op-group" list="op-groups" value={form.group} placeholder="None" onChange={(e) => setForm({ ...form, group: e.target.value })} />
          <datalist id="op-groups">{groups.map((g) => <option key={g} value={g} />)}</datalist><FieldError errors={errors} name="group" hint="Groups share permissions." /></div>
        {soap ? <div className="field"><label htmlFor="op-action">SOAP action</label><input id="op-action" className="mono" value={form.soap_action} placeholder="urn:example/GetCustomer" onChange={(e) => setForm({ ...form, soap_action: e.target.value })} />
          <FieldError errors={errors} name="soap_action" hint={internal ? "Matched on the SOAPAction header, else the body element name." : "Sent as the SOAPAction."} /></div>
          : <div className="field"><label htmlFor="op-method">Method</label><select id="op-method" value={form.method} onChange={(e) => setForm({ ...form, method: e.target.value })}>{["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => <option key={m}>{m}</option>)}</select><FieldError errors={errors} name="method" /></div>}
        {(!soap || !internal) && <div className="field"><label htmlFor="op-path">{soap ? "Backend path" : "Path"}</label><input id="op-path" className="mono" value={form.path} placeholder="/customers/{id}" onChange={(e) => setForm({ ...form, path: e.target.value })} />
          <FieldError errors={errors} name="path" hint={soap ? "Where the backend's SOAP endpoint lives, relative to the base URL." : "{name} marks a parameter."} /></div>}
      </div>
      <div className="field" style={{ maxWidth: 720 }}><label htmlFor="op-description">Description</label><input id="op-description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></div>
      {internal && form.internal && <>
        <h3>Runs</h3>
        <div className="arg-grid">
          <div className="field" style={{ gridColumn: "span 2" }}><label htmlFor="op-program">Deployed program *</label>
            <select id="op-program" value={form.internal.workload_id} onChange={(e) => setInternal({ workload_id: e.target.value, args: {} })}>
              <option value="">Choose a deployed program</option>
              {programs.map((p) => <option key={p.id} value={p.id}>{p.title} ({p.kind}, {p.repo}@{p.branch})</option>)}
            </select><FieldError errors={errors} name="internal.workload_id" hint="Only on-demand programs you may run yourself. Never data endpoints." /></div>
          <div className="field"><label htmlFor="op-mode">Response</label><select id="op-mode" value={form.internal.mode} onChange={(e) => setInternal({ mode: e.target.value as "async" | "sync" })}>
            <option value="async">Asynchronous (202 + status URL)</option><option value="sync">Wait for the result</option></select></div>
          {form.internal.mode === "sync" && <div className="field"><label htmlFor="op-wait">Wait up to (s)</label><input id="op-wait" type="number" min={1} max={120} value={form.internal.timeout_seconds} onChange={(e) => setInternal({ timeout_seconds: Number(e.target.value) })} /></div>}
          <div className="field"><label htmlFor="op-pool">Warehouse</label><select id="op-pool" value={form.internal.pool} onChange={(e) => setInternal({ pool: e.target.value })}>
            <option value="">Program default</option>{usablePools(hub.me?.pools).map((p) => <option key={p.id} value={p.name}>{p.name}</option>)}</select></div>
        </div>
        <p className="hint">The program answers with <code>fdp.respond(data, status=200)</code>: <code>data</code> becomes the result and <code>status</code> the HTTP status code (e.g. 404 to answer with an error). {form.internal.mode === "sync" ? "Callers get that status code directly." : "Callers poll the status URL; its answer includes status_code."}</p>
        {program && (program.program_def.args ?? []).length > 0 && <>
          <h3>Arguments</h3>
          <table className="values-table"><thead><tr><th>Program argument</th><th>From the request</th></tr></thead><tbody>
            {(program.program_def.args ?? []).map((a) => <tr key={a.name}><td className="mono">{a.name}{a.required ? " *" : ""} <span className="muted">{a.type}</span></td>
              <td><input aria-label={`Source of ${a.name}`} className="mono" value={form.internal!.args[a.name] ?? ""} placeholder={soap ? `body.${a.name}` : `path, query or body "${a.name}"`}
                onChange={(e) => setInternal({ args: { ...form.internal!.args, [a.name]: e.target.value } })} /><FieldError errors={errors} name={`internal.args.${a.name}`} /></td></tr>)}
          </tbody></table>
          <p className="hint">Use path.x, query.x, body.x (dots for nesting), header.x or =value. Empty takes the same name from the path, query or body.</p>
        </>}
      </>}
      {!internal && form.external && <>
        <ValuesEditor label="Extra headers" prefix="external.headers" namePlaceholder="X-Operation" values={form.external.headers} errors={errors} onChange={(headers) => setForm({ ...form, external: { ...form.external!, headers } })} />
      </>}
      {editable && <div className="form-actions"><button className="btn" disabled={busy || !form.name.trim()}>{busy ? "Saving…" : op ? "Save operation" : "Add operation"}</button></div>}
    </fieldset>
  </form>;
}

function TryIt({ item, op }: { item: ApiDefinition; op: ApiOperation }) {
  const soap = item.protocol === "soap";
  const [path, setPath] = useState(soap ? op.name : op.path);
  const [body, setBody] = useState(soap ? `<${op.name} xmlns="">\n</${op.name}>` : "");
  const [result, setResult] = useState<{ status: number; headers: Record<string, string>; body: string; ms: number } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const canCall = op.actions.includes("call");
  const send = async () => {
    setBusy(true); setError("");
    try { setResult(await tryExternal(item.slug, soap ? "POST" : op.method, path, body, body ? { "Content-Type": soap ? "text/xml" : "application/json" } : {})); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  let pretty = result?.body ?? "";
  try { if (result && result.headers["content-type"]?.includes("json")) pretty = JSON.stringify(JSON.parse(result.body), null, 2); } catch { /* raw */ }
  return <section className="card" aria-label="Try it">
    <h2>Try it</h2>
    <p className="hint" style={{ marginTop: -4 }}>Calls the backend through Floren Data Platform as you, with the API's configured credentials. Programs use
      <code>{soap ? ` fdp.api("${item.slug}").soap("${op.name}", body)` : ` fdp.api("${item.slug}").${op.method.toLowerCase()}("${op.path}")`}</code>.</p>
    {!canCall ? <p className="notice">Calling requires the Consumer role on this operation, its group or the API.</p> : <>
      <div className="row" style={{ alignItems: "flex-end" }}>
        <span className={methodClass(soap ? "POST" : op.method)} style={{ marginBottom: 5 }}>{soap ? "SOAP" : op.method}</span>
        <div className="field" style={{ flex: 1, margin: 0 }}><label htmlFor="try-path">{soap ? "Operation" : "Path"}</label><input id="try-path" className="mono" value={path} disabled={soap} onChange={(e) => setPath(e.target.value)} /></div>
        <button className="btn" disabled={busy} onClick={send}><IconPlay size={14} />{busy ? "Sending…" : "Send"}</button>
      </div>
      {(soap || op.method !== "GET") && <div className="field" style={{ marginTop: 8 }}><label htmlFor="try-body">{soap ? "Body element" : "JSON body"}</label><textarea id="try-body" className="short" value={body} onChange={(e) => setBody(e.target.value)} /></div>}
    </>}
    {error && <p className="error" role="alert">{error}</p>}
    {result && <div aria-label="Response">
      <p className="row"><span className={`badge ${result.status < 300 ? "" : "warn"}`}>{result.status}</span><span className="muted">{result.ms} ms</span></p>
      <pre className="code" style={{ maxHeight: 320 }}>{pretty || "(empty)"}</pre>
    </div>}
  </section>;
}

function CallIt({ item, op, hub }: { item: ApiDefinition; op: ApiOperation; hub: Hub }) {
  const inbound = item.config.inbound;
  const program = hub.workloads.find((p) => p.id === op.config.workload_id);
  const auth = [inbound?.mtls.enabled && "--cert client.pem --key client-key.pem", inbound?.jwt.enabled && '-H "Authorization: Bearer $TOKEN"'].filter(Boolean).join(" ");
  const example = item.protocol === "soap"
    ? `curl -X POST ${item.endpoint} ${auth} \\\n  -H 'Content-Type: text/xml' -H 'SOAPAction: "${op.soap_action || op.name}"' \\\n  -d '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><${op.name}>…</${op.name}></soap:Body></soap:Envelope>'`
    : `curl -X ${op.method} ${item.endpoint}${op.path} ${auth}${op.method !== "GET" ? " \\\n  -H 'Content-Type: application/json' -d '{…}'" : ""}`;
  return <section className="card" aria-label="Call it">
    <h2>Call it</h2>
    <p className="hint" style={{ marginTop: -4 }}>Runs <b>{program?.title ?? "the deployed program"}</b> as {item.identity?.name ?? "the API's managed identity"}.
      {op.config.mode === "sync" ? " Waits for the result." : ` Returns 202 with a status URL (${item.endpoint}/_runs/<run id>), which the same caller can poll.`}</p>
    <pre className="code">{example}</pre>
  </section>;
}

function CallsPanel({ item }: { item: ApiDefinition }) {
  const [calls, setCalls] = useState<ApiCallRecord[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { api.apis.calls(item.id).then(setCalls).catch((e: Error) => setError(e.message)); }, [item.id]);
  return <>
    {error && <p className="error" role="alert">{error}</p>}
    <table aria-label="Recent calls"><thead><tr><th>When</th><th>Operation</th><th>Caller</th><th>Run</th></tr></thead><tbody>
      {(calls ?? []).map((c) => <tr key={c.id}><td className="muted">{new Date(c.at).toLocaleString()}</td><td className="mono">{c.operation}</td><td className="mono">{c.caller}</td>
        <td>{c.run_id ? <><span className="badge">{c.run_status ?? "run"}</span> <span className="mono">{c.run_id}</span></> : "—"}</td></tr>)}
      {calls && !calls.length && <tr><td colSpan={4}>No calls yet. Denied calls are recorded in the audit log.</td></tr>}
    </tbody></table>
  </>;
}
