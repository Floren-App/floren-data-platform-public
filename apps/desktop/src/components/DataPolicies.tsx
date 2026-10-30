import { FormEvent, useEffect, useState } from "react";
import { AccessResource, AppGroup, DataPolicy, DirectoryPrincipal, api } from "../api";

const METHODS: Record<string, string> = {
  null: "Show NULL",
  redact: "Show ****",
  hash: "Show a SHA-256 hash (equal values stay equal)",
  partial: "Show the last 4 characters",
  expression: "Custom SQL expression",
};

type Draft = {
  id?: string; table: string; kind: DataPolicy["kind"]; column: string; method: DataPolicy["method"];
  expression: string; exempt_groups: string[]; exempt_principals: string[]; description: string;
};

const EMPTY: Draft = { table: "", kind: "column_mask", column: "", method: "partial", expression: "",
  exempt_groups: [], exempt_principals: [], description: "" };

function rule(policy: DataPolicy) {
  if (policy.kind === "row_filter") return <code>{policy.expression}</code>;
  return policy.method === "expression" ? <code>{policy.expression}</code> : METHODS[policy.method] ?? policy.method;
}

/** Column masks and row filters of one SQL schema. Owners change them; readers see them. */
export function DataPoliciesPanel({ schema }: { schema: AccessResource }) {
  const manage = schema.actions.includes("assign");
  const [policies, setPolicies] = useState<DataPolicy[] | null>(null);
  const [groups, setGroups] = useState<AppGroup[]>([]);
  const [identities, setIdentities] = useState<DirectoryPrincipal[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try { setPolicies(await api.dataPolicies.list(schema.id)); }
    catch (e) { setError((e as Error).message); setPolicies([]); }
  };
  useEffect(() => { load(); }, [schema.id]);
  useEffect(() => {
    if (!manage) return;
    api.access.groups().then(setGroups).catch(() => undefined);
    api.access.directory().then((all) => setIdentities(all.filter((p) => p.kind === "user" || p.kind === "service"))).catch(() => undefined);
  }, [manage]);

  const groupName = (id: string) => groups.find((g) => g.id === id)?.name ?? id;
  const identityName = (id: string) => { const p = identities.find((x) => x.id === id); return p ? p.display_name || p.name : id; };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    setBusy(true); setError("");
    const change = { method: draft.kind === "row_filter" ? "" as const : draft.method,
      expression: draft.kind === "row_filter" || draft.method === "expression" ? draft.expression : "",
      exempt_groups: draft.exempt_groups, exempt_principals: draft.exempt_principals, description: draft.description };
    try {
      if (draft.id) await api.dataPolicies.update(draft.id, change);
      else await api.dataPolicies.create({ ...change, schema_id: schema.id, table: draft.table, kind: draft.kind,
        column: draft.kind === "column_mask" ? draft.column : "" });
      setDraft(null);
      await load();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };

  const remove = async (policy: DataPolicy) => {
    const what = policy.kind === "row_filter" ? `the row filter on ${policy.table}` : `the mask on ${policy.table}.${policy.column}`;
    if (!confirm(`Remove ${what}? People will see the real values or all rows in queries that start afterwards.`)) return;
    setError("");
    try { await api.dataPolicies.remove(policy.id); await load(); }
    catch (e) { setError((e as Error).message); }
  };

  const toggle = (list: string[], id: string) => list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
  const expressionShown = draft && (draft.kind === "row_filter" || draft.method === "expression");

  return <section className="card" aria-labelledby="policies-heading">
    <div className="section-heading" style={{ marginTop: 0 }}><div>
      <h2 id="policies-heading">Masks and filters <span className="badge">{policies?.length ?? 0}</span></h2>
      <p className="muted">A column mask hides or transforms a column's values; a row filter limits the rows people see.
        They apply to everyone querying the table, in every tool, except the exempt groups and identities.
        Changes apply to queries that start afterwards.</p></div>
      {manage && !draft && <button className="btn secondary" onClick={() => setDraft({ ...EMPTY })}>Add mask or filter</button>}
    </div>
    {error && <p className="error" role="alert">{error}</p>}

    {draft && <form className="card" onSubmit={save} aria-label={draft.id ? "Change policy" : "Add policy"}>
      <h3>{draft.id ? `Change ${draft.kind === "row_filter" ? "row filter" : "column mask"} on ${draft.table}` : "Add a mask or filter"}</h3>
      {!draft.id && <div className="arg-grid">
        <div className="field"><label htmlFor="policy-table">Table</label>
          <input id="policy-table" required value={draft.table} placeholder="customers" onChange={(e) => setDraft({ ...draft, table: e.target.value })} /></div>
        <div className="field"><label htmlFor="policy-kind">Kind</label>
          <select id="policy-kind" value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value as Draft["kind"] })}>
            <option value="column_mask">Column mask</option><option value="row_filter">Row filter</option></select></div>
        {draft.kind === "column_mask" && <div className="field"><label htmlFor="policy-column">Column</label>
          <input id="policy-column" required value={draft.column} placeholder="email" onChange={(e) => setDraft({ ...draft, column: e.target.value })} /></div>}
      </div>}
      {draft.kind === "column_mask" && <div className="field"><label htmlFor="policy-method">What people see</label>
        <select id="policy-method" value={draft.method} onChange={(e) => setDraft({ ...draft, method: e.target.value as Draft["method"] })}>
          {Object.entries(METHODS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>
        <span className="hint">****, hash and last 4 characters apply to text columns; other types show NULL.</span></div>}
      {expressionShown && <div className="field"><label htmlFor="policy-expression">{draft.kind === "row_filter" ? "Rows people see (condition)" : "Expression"}</label>
        <textarea id="policy-expression" className="short mono" required value={draft.expression}
          placeholder={draft.kind === "row_filter" ? "region = 'EU' OR is_member('Global analysts')" : "date_trunc('year', birth_date)"}
          onChange={(e) => setDraft({ ...draft, expression: e.target.value })} />
        <span className="hint">Trino SQL over this table's columns. <code>is_member('group name')</code> is true for members of that app group.
          No subqueries or other tables.</span></div>}
      <fieldset className="field"><legend>Exempt groups (see real values and all rows)</legend>
        {groups.length ? groups.map((g) => <label key={g.id} className="row"><input type="checkbox" checked={draft.exempt_groups.includes(g.id)}
          onChange={() => setDraft({ ...draft, exempt_groups: toggle(draft.exempt_groups, g.id) })} />{g.name}</label>)
          : <span className="muted">No app groups.</span>}</fieldset>
      <div className="field"><label htmlFor="policy-identity">Exempt identities</label>
        <select id="policy-identity" value="" onChange={(e) => e.target.value && setDraft({ ...draft, exempt_principals: toggle(draft.exempt_principals, e.target.value) })}>
          <option value="">Add a person or managed identity…</option>
          {identities.filter((p) => !draft.exempt_principals.includes(p.id)).map((p) =>
            <option key={p.id} value={p.id}>{(p.display_name || p.name) + (p.kind === "service" ? " (managed identity)" : "")}</option>)}</select>
        {draft.exempt_principals.length > 0 && <ul className="plain">{draft.exempt_principals.map((id) => <li key={id}>{identityName(id)}{" "}
          <button type="button" className="text-button" onClick={() => setDraft({ ...draft, exempt_principals: toggle(draft.exempt_principals, id) })}>Remove</button></li>)}</ul>}
        <span className="hint">For example a pipeline's managed identity that must process the real data.</span></div>
      <div className="field"><label htmlFor="policy-description">Description</label>
        <input id="policy-description" value={draft.description} placeholder="Why this is protected" onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></div>
      <div className="form-actions">
        <button className="btn" disabled={busy}>{busy ? "Saving…" : draft.id ? "Save" : "Add"}</button>
        <button type="button" className="btn secondary" onClick={() => { setDraft(null); setError(""); }}>Cancel</button></div>
    </form>}

    {policies === null ? <p className="muted">Loading…</p> : <div className="table-scroll"><table>
      <thead><tr><th>Table</th><th>Applies to</th><th>What people see</th><th>Exempt</th>{manage && <th />}</tr></thead>
      <tbody>{policies.map((p) => <tr key={p.id}>
        <td className="mono">{p.table}</td>
        <td>{p.kind === "row_filter" ? "Rows" : <span className="mono">{p.column}</span>}</td>
        <td>{rule(p)}{p.description && <div className="muted">{p.description}</div>}</td>
        <td>{[...p.exempt_groups.map(groupName), ...p.exempt_principals.map(identityName)].join(", ") || <span className="muted">Nobody</span>}</td>
        {manage && <td className="table-actions">
          <button className="btn sm secondary" onClick={() => setDraft({ id: p.id, table: p.table, kind: p.kind, column: p.column, method: p.method,
            expression: p.expression, exempt_groups: p.exempt_groups, exempt_principals: p.exempt_principals, description: p.description })}>Change</button>
          <button className="btn sm secondary" onClick={() => remove(p)}>Remove</button></td>}
      </tr>)}
        {!policies.length && <tr><td colSpan={manage ? 5 : 4} className="muted">No masks or filters: everyone with access sees all rows and values.</td></tr>}
      </tbody></table></div>}
  </section>;
}
