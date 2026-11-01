import { useCallback, useEffect, useState } from "react";
import { AccessResource, AlertRuleInput, AlertRuleItem, api } from "../api";
import type { Hub } from "../hub";
import { resourcePath } from "../components/Permissions";
import { IconPlus, IconTrash } from "../components/Icons";

const SCOPABLE = ["root", "folder", "service", "source", "schema", "kafka", "topic", "product", "api"];

/** Alert rules: which events go to which channel, for which layers, objects and classifications. With a resource:
 * the rules on that object, as its owners see them on its Alerts tab. */
export function AlertRules({ hub, resource }: { hub: Hub; resource?: AccessResource }) {
  const [rules, setRules] = useState<AlertRuleItem[] | null>(null), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [channels, setChannels] = useState<{ id: string; name: string; enabled: boolean }[]>([]);
  const [catalog, setCatalog] = useState<{ events: { key: string; label: string }[]; groups: Record<string, string[]> } | null>(null);
  const empty = (): AlertRuleInput => ({ name: "", events: [], channel_id: "", layer_ids: [], resource_ids: resource ? [resource.id] : [], classifications: [], enabled: true });
  const [draft, setDraft] = useState<AlertRuleInput | null>(null);
  const load = useCallback(() => api.alerts.rules(resource?.id ?? "").then(setRules).catch((e: Error) => setError(e.message)), [resource?.id]);
  useEffect(() => { void load(); api.alerts.channels().then(setChannels).catch(() => setChannels([]));
    api.alerts.events().then(setCatalog).catch(() => setCatalog(null)); }, [load]);
  const label = (key: string) => catalog?.events.find((e) => e.key === key)?.label ?? key;
  const toggle = (list: string[], value: string) => list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
  const save = (e: React.FormEvent) => { e.preventDefault(); if (!draft) return; setError("");
    api.alerts.createRule({ ...draft, name: draft.name.trim() }).then(() => { setNotice(`Added ${draft.name.trim()}.`); setDraft(null); void load(); })
      .catch((err: Error) => setError(err.message)); };
  const act = (p: Promise<unknown>, text: string) => { setError(""); p.then(() => { setNotice(text); void load(); }).catch((err: Error) => setError(err.message)); };
  const scopable = hub.resources.filter((r) => SCOPABLE.includes(r.kind)).sort((a, b) => resourcePath(a, hub.resources).localeCompare(resourcePath(b, hub.resources)));
  const scope = (r: AlertRuleItem) => [r.layer_ids.length ? r.layer_ids.map((id) => hub.layers.find((l) => l.id === id)?.name ?? id).join(", ") : "",
    r.objects.length ? r.objects.map((o) => o.name).join(", ") : "", r.classifications.join(", ")].filter(Boolean).join(" · ") || "Everything";
  return <section aria-label={resource ? `Alerts for ${resource.name}` : "Alert rules"}>
    {notice && <p className="success-message" role="status">{notice}</p>}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="table-scroll"><table aria-label="Alert rules"><thead><tr><th>Rule</th><th>Events</th><th>For</th><th>To</th><th /></tr></thead><tbody>
      {(rules ?? []).map((r) => <tr key={r.id}>
        <td><strong>{r.name}</strong>{!r.enabled && <span className="badge"> Off</span>}<div className="muted small">by {r.created_by}</div></td>
        <td className="small">{r.events.map(label).join(", ")}</td><td className="small">{scope(r)}</td><td>{r.channel ?? "—"}</td>
        <td className="table-actions"><div className="row" style={{ gap: 4 }}>
          <button className="btn sm secondary" onClick={() => act(api.alerts.updateRule(r.id, { ...r, enabled: !r.enabled }), `${r.enabled ? "Paused" : "Resumed"} ${r.name}.`)}>{r.enabled ? "Pause" : "Resume"}</button>
          <button className="icon-btn" aria-label={`Remove ${r.name}`} onClick={() => act(api.alerts.deleteRule(r.id), `Removed ${r.name}.`)}><IconTrash size={14} /></button></div></td>
      </tr>)}
      {rules && !rules.length && <tr><td colSpan={5} className="muted">{resource ? "No alerts for this object yet." : "No rules yet."}</td></tr>}</tbody></table></div>
    {!draft ? <div className="form-actions"><button className="btn secondary" disabled={!channels.length} onClick={() => setDraft(empty())}><IconPlus size={14} />Add alert</button>
      {!channels.length && <span className="hint">An administrator first adds a channel under Administration → Alerts.</span>}</div>
      : <form className="card" aria-label="New alert" onSubmit={save}>
        <h3>New alert</h3>
        <div className="arg-grid">
          <div className="field"><label htmlFor="rule-name">Name</label><input id="rule-name" required maxLength={120} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="Failed runs in Prod" /></div>
          <div className="field"><label htmlFor="rule-channel">Send to</label><select id="rule-channel" required value={draft.channel_id} onChange={(e) => setDraft({ ...draft, channel_id: e.target.value })}>
            <option value="">Choose a channel</option>{channels.map((c) => <option key={c.id} value={c.id}>{c.name}{c.enabled ? "" : " (off)"}</option>)}</select></div>
        </div>
        <fieldset className="field form-choices"><legend>Events</legend>
          {Object.entries(catalog?.groups ?? {}).map(([group, keys]) => <div key={group}><div className="muted small">{group}</div>
            {keys.map((key) => <label key={key} className="row"><input type="checkbox" checked={draft.events.includes(key)} onChange={() => setDraft({ ...draft, events: toggle(draft.events, key) })} />
              {label(key)}</label>)}</div>)}</fieldset>
        <fieldset className="field form-choices"><legend>Only in these layers</legend><span className="hint">None chosen: every layer.</span>
          {hub.layers.map((l) => <label key={l.id} className="row"><input type="checkbox" checked={draft.layer_ids.includes(l.id)} onChange={() => setDraft({ ...draft, layer_ids: toggle(draft.layer_ids, l.id) })} />{l.name}</label>)}</fieldset>
        {!resource && <div className="field"><label htmlFor="rule-objects">Only for these objects, and what is under them</label>
          <select id="rule-objects" multiple size={6} value={draft.resource_ids} onChange={(e) => setDraft({ ...draft, resource_ids: Array.from(e.target.selectedOptions, (o) => o.value) })}>
            {scopable.map((r) => <option key={r.id} value={r.id}>{resourcePath(r, hub.resources) || r.name}</option>)}</select>
          <span className="hint">None chosen: everything. Hold ⌘/Ctrl to choose several.</span></div>}
        <div className="form-actions"><button className="btn" disabled={!draft.name.trim() || !draft.channel_id || !draft.events.length}>Add alert</button>
          <button type="button" className="btn secondary" onClick={() => setDraft(null)}>Cancel</button></div>
      </form>}
  </section>;
}
