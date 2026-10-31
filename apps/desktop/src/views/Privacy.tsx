import { useCallback, useEffect, useState } from "react";
import { AccessCaseItem, AccessFormItem, AccessPolicyItem, api, JustifyDeadline, RegisterRow, saveFile, AppGroup, ClassificationLevel, ComplianceRoleItem, FieldType, FormCondition, FormDefinition, FormField, FormPage } from "../api";
import type { Hub } from "../hub";
import { PageHeader } from "../components/Page";
import { ProductIcon } from "../components/ProductIcon";
import { IconPlus, IconTrash } from "../components/Icons";
import { Answers, FormRenderer } from "../components/ComplianceForm";
import type { TabRef } from "./Tabs";
import { CaseTable, overdue } from "./Cases";

const STEP_LABEL = { data_owner: "Data owners", compliance_reviewer: "Compliance reviewers" } as const;
export const isComplianceAdmin = (hub: Hub) => !!hub.me?.compliance_roles?.includes("compliance_admin");

/** Which form each layer and classification needs. An empty cell needs no form. */
export function PrivacyPolicies({ hub }: { hub: Hub }) {
  const admin = isComplianceAdmin(hub);
  const [grid, setGrid] = useState<{ layers: { id: string; name: string }[]; classifications: ClassificationLevel[]; policies: AccessPolicyItem[] } | null>(null);
  const [forms, setForms] = useState<AccessFormItem[]>([]), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [cell, setCell] = useState<{ layer: string; level: string } | null>(null);
  const load = useCallback(() => Promise.all([api.compliance.policies(), api.compliance.forms()])
    .then(([g, f]) => { setGrid(g); setForms(f); }).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void load(); }, [load]);
  if (!grid) return error ? <p className="error" role="alert">{error}</p> : <p className="muted" role="status">Loading…</p>;
  const policy = (layer: string, level: string) => grid.policies.find((p) => p.layer_id === layer && p.classification === level);
  const lowest = grid.classifications[0]?.name;
  return <>
    <PageHeader icon={<ProductIcon kind="identity" size={22} />} title="Access policies"
      subtitle="Which compliance form access needs, per layer and classification. Every way of giving access to data in a filled cell needs an approved form: requests, direct grants, product consumers, and Terraform or the API." />
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="scope-note" role="status">{message}</p>}
    <div className="table-scroll"><table className="policy-grid" aria-label="Access policies"><thead><tr><th>Classification</th>{grid.layers.map((l) => <th key={l.id}>{l.name}</th>)}</tr></thead>
      <tbody>{[...grid.classifications].reverse().map((c) => <tr key={c.name}><th scope="row">{c.name}{c.masked_as !== c.name && <div className="muted small">masked: counts as {c.masked_as}</div>}</th>
        {grid.layers.map((l) => { const p = policy(l.id, c.name), open = cell?.layer === l.id && cell.level === c.name;
          const label = p ? `${p.form ?? "Form"}: ${p.steps.map((s) => STEP_LABEL[s.role]).join(" → ")}, at most ${p.max_days} days` : "No form needed";
          return <td key={l.id} className={`${p ? "policy-cell filled" : "policy-cell"}${open ? " selected" : ""}`}>
            {c.name === lowest ? <span className="muted">Never</span>
              : <button className="text-button" disabled={!admin && !p} aria-label={`${l.name}, ${c.name}: ${label}`} onClick={() => setCell(open ? null : { layer: l.id, level: c.name })}>
                {p ? <><strong>{p.form}</strong><div className="muted small">{p.steps.map((s) => STEP_LABEL[s.role] + (s.approvals > 1 ? ` ×${s.approvals}` : "")).join(" → ")}</div>
                  <div className="muted small">at most {p.max_days} days</div></> : <span className="muted">No form</span>}</button>}</td>; })}</tr>)}</tbody></table></div>
    {cell && <PolicyEditor key={`${cell.layer}/${cell.level}`} layer={grid.layers.find((l) => l.id === cell.layer)!} level={cell.level} policy={policy(cell.layer, cell.level)}
      forms={forms} readOnly={!admin} onClose={() => setCell(null)} onSaved={(text) => { setMessage(text); setCell(null); void load(); }} />}
    <p className="hint">The lowest classification never needs a form. Masked columns count as their masked level, so a product option that masks personal data can fall in a cell without a form.
      Also configurable with Terraform (fdp_access_policy).</p>
  </>;
}

function PolicyEditor({ layer, level, policy, forms, readOnly, onClose, onSaved }: { layer: { id: string; name: string }; level: string; policy?: AccessPolicyItem;
  forms: AccessFormItem[]; readOnly: boolean; onClose: () => void; onSaved: (message: string) => void }) {
  const published = forms.filter((f) => f.published_version !== null);
  const [form, setForm] = useState(policy?.form_id ?? published[0]?.id ?? "");
  const [steps, setSteps] = useState<AccessPolicyItem["steps"]>(policy?.steps ?? [{ role: "compliance_reviewer", approvals: 1 }]);
  const [maxDays, setMaxDays] = useState(policy?.max_days ?? 365), [reviewDays, setReviewDays] = useState(policy?.review_days ?? 10), [error, setError] = useState("");
  const [renewDays, setRenewDays] = useState(policy?.renew_days ?? 30), [graceDays, setGraceDays] = useState(policy?.grace_days ?? 30);
  const save = () => { setError("");
    api.compliance.putPolicy(layer.id, level, { form_id: form, steps, max_days: maxDays, review_days: reviewDays, renew_days: renewDays, grace_days: graceDays })
      .then(() => onSaved(`${layer.name}, ${level}: access needs a form.`)).catch((e: Error) => setError(e.message)); };
  const remove = () => { setError("");
    api.compliance.deletePolicy(layer.id, level).then(() => onSaved(`${layer.name}, ${level}: no form needed any more.`)).catch((e: Error) => setError(e.message)); };
  const setStep = (i: number, patch: Partial<AccessPolicyItem["steps"][number]>) => setSteps(steps.map((s, j) => j === i ? { ...s, ...patch } : s));
  return <section className="card policy-editor" aria-label={`Policy for ${layer.name}, ${level}`}>
    <h2>{layer.name} · {level}</h2>
    <div className="arg-grid">
      <div className="field"><label htmlFor="policy-form">Form</label>
        <select id="policy-form" value={form} disabled={readOnly} onChange={(e) => setForm(e.target.value)}>
          {!published.length && <option value="">Publish a form first</option>}
          {published.map((f) => <option key={f.id} value={f.id}>{f.name} (version {f.published_version})</option>)}</select></div>
      <div className="field"><label htmlFor="policy-max">Access lasts at most (days)</label>
        <input id="policy-max" type="number" min={1} max={3650} disabled={readOnly} value={maxDays} onChange={(e) => setMaxDays(Number(e.target.value))} /></div>
      <div className="field"><label htmlFor="policy-review">Reviews are due within (days)</label>
        <input id="policy-review" type="number" min={1} max={365} disabled={readOnly} value={reviewDays} onChange={(e) => setReviewDays(Number(e.target.value))} /></div>
      <div className="field"><label htmlFor="policy-renew">Renewal opens before the end (days)</label>
        <input id="policy-renew" type="number" min={1} max={365} disabled={readOnly} value={renewDays} onChange={(e) => setRenewDays(Number(e.target.value))} /></div>
      <div className="field"><label htmlFor="policy-grace">Existing access must be justified within (days)</label>
        <input id="policy-grace" type="number" min={1} max={365} disabled={readOnly} value={graceDays} onChange={(e) => setGraceDays(Number(e.target.value))} />
        <span className="hint">For access that existed before this policy, or whose data was classified higher later. Without an approved form by then, it is removed.</span></div>
    </div>
    <fieldset className="field"><legend>Review steps, in order</legend>
      <p className="hint">Data owners are the owners of the product or object. Compliance reviewers are groups with that role for this layer and classification.</p>
      {steps.map((s, i) => <div key={i} className="row" style={{ gap: 8, marginBottom: 6 }}>
        <span className="muted">{i + 1}.</span>
        <select aria-label={`Step ${i + 1} reviewers`} value={s.role} disabled={readOnly} onChange={(e) => setStep(i, { role: e.target.value as typeof s.role })}>
          {Object.entries(STEP_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <label className="row">Approvals<input aria-label={`Step ${i + 1} approvals`} type="number" min={1} max={5} style={{ width: 60 }} disabled={readOnly} value={s.approvals}
          onChange={(e) => setStep(i, { approvals: Number(e.target.value) })} /></label>
        {!readOnly && <button type="button" className="icon-btn" aria-label={`Remove step ${i + 1}`} disabled={steps.length === 1} onClick={() => setSteps(steps.filter((_, j) => j !== i))}><IconTrash size={14} /></button>}
      </div>)}
      {!readOnly && steps.length < 5 && <button type="button" className="btn sm secondary" onClick={() => setSteps([...steps, { role: "compliance_reviewer", approvals: 1 }])}><IconPlus size={14} />Add step</button>}
    </fieldset>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="form-actions">
      {!readOnly && <button className="btn" disabled={!form} onClick={save}>Save policy</button>}
      {!readOnly && policy && <button className="btn danger" onClick={remove}>No form needed</button>}
      <button className="btn secondary" onClick={onClose}>Close</button></div>
  </section>;
}

/** The forms: each has versions; cases keep the version they were filled in with. */
export function PrivacyForms({ hub, openTab }: { hub: Hub; openTab: (t: TabRef) => void }) {
  const admin = isComplianceAdmin(hub);
  const [forms, setForms] = useState<AccessFormItem[] | null>(null), [error, setError] = useState(""), [name, setName] = useState("");
  useEffect(() => { api.compliance.forms().then(setForms).catch((e: Error) => setError(e.message)); }, []);
  const create = (e: React.FormEvent) => { e.preventDefault(); setError("");
    api.compliance.saveForm(name.trim(), { name: name.trim(), description: "", definition: { pages: [newPage([])] } })
      .then((f) => { setName(""); openTab({ kind: "privacy-form", ref: f.id, title: f.name }); }).catch((err: Error) => setError(err.message)); };
  return <>
    <PageHeader icon={<ProductIcon kind="file" size={22} />} title="Compliance forms"
      subtitle="What people fill in to justify access. Forms have pages, and questions or pages can depend on earlier answers. Changes are drafted, then published as a new version." />
    {error && <p className="error" role="alert">{error}</p>}
    {!forms ? <p className="muted" role="status">Loading…</p> : <div className="table-scroll"><table aria-label="Forms"><thead><tr><th>Form</th><th>Published</th><th>Draft</th><th>Used by</th></tr></thead><tbody>
      {forms.map((f) => <tr key={f.id}><td><button className="text-button" onClick={() => openTab({ kind: "privacy-form", ref: f.id, title: f.name })}>{f.name}</button>
        {f.description && <div className="muted small">{f.description}</div>}</td>
        <td>{f.published_version ? `Version ${f.published_version}` : <span className="muted">Not yet</span>}</td>
        <td>{f.has_draft ? <span className="badge">Draft</span> : <span className="muted">—</span>}</td>
        <td>{f.used_by ? `${f.used_by} polic${f.used_by === 1 ? "y" : "ies"}` : <span className="muted">No policy</span>}</td></tr>)}
      {!forms.length && <tr><td colSpan={4} className="muted">No forms yet.</td></tr>}</tbody></table></div>}
    {admin && <form className="row" style={{ gap: 8, marginTop: 12 }} onSubmit={create}>
      <input aria-label="New form name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Personal data access" maxLength={120} />
      <button className="btn" disabled={!name.trim()}><IconPlus size={14} />New form</button></form>}
    <p className="hint">Also configurable with Terraform (fdp_access_form).</p>
  </>;
}

const TYPES: { key: FieldType; label: string }[] = [
  { key: "short_text", label: "Short text" }, { key: "long_text", label: "Long text" }, { key: "choice", label: "One choice" },
  { key: "multi_choice", label: "Several choices" }, { key: "date", label: "Date" }, { key: "number", label: "Number" },
  { key: "statement", label: "Statement to agree to" }, { key: "person", label: "Person" }, { key: "file", label: "File" }];

function freeId(prefix: string, taken: string[]) {
  let n = 1;
  while (taken.includes(`${prefix}${n}`)) n++;
  return `${prefix}${n}`;
}
const allFields = (d: FormDefinition) => d.pages.flatMap((p) => p.fields);
const newPage = (pages: FormPage[]): FormPage => ({ id: freeId("page", pages.map((p) => p.id)), title: "New page", intro: "", show_if: null, fields: [] });

/** One form: the builder for its draft, a preview, and its versions. */
export function PrivacyForm({ id, hub, onRenamed, onDeleted }: { id: string; hub: Hub; onRenamed: (name: string) => void; onDeleted: () => void }) {
  const admin = isComplianceAdmin(hub);
  const [form, setForm] = useState<AccessFormItem | null>(null), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [name, setName] = useState(""), [description, setDescription] = useState(""), [definition, setDefinition] = useState<FormDefinition>({ pages: [] });
  const [dirty, setDirty] = useState(false), [preview, setPreview] = useState(false), [answers, setAnswers] = useState<Answers>({});
  const show = (f: AccessFormItem) => {
    setForm(f); setName(f.name); setDescription(f.description); setDirty(false);
    const latest = f.versions?.[f.versions.length - 1];
    setDefinition(latest?.definition ?? { pages: [newPage([])] });
  };
  useEffect(() => { api.compliance.form(id).then(show).catch((e: Error) => setError(e.message)); }, [id]);
  if (!form) return error ? <p className="error" role="alert">{error}</p> : <p className="muted" role="status">Loading…</p>;
  const change = (d: FormDefinition) => { setDefinition(d); setDirty(true); };
  const save = () => { setError(""); setMessage("");
    return api.compliance.saveForm(form.name, { name: name.trim(), description, definition }).then((f) => { show(f); if (f.name !== form.name) onRenamed(f.name); setMessage("Draft saved."); return f; }); };
  const publish = () => { setError(""); setMessage("");
    (dirty || !form.has_draft ? save() : Promise.resolve(form)).then((f) => api.compliance.publishForm(f.id))
      .then(() => api.compliance.form(id)).then((f) => { show(f); setMessage(`Published version ${f.published_version}. New cases use it; open cases keep theirs.`); })
      .catch((e: Error) => setError(e.message)); };
  const remove = () => { setError(""); api.compliance.deleteForm(form.id).then(onDeleted).catch((e: Error) => setError(e.message)); };
  const versions = form.versions ?? [];
  return <>
    <PageHeader icon={<ProductIcon kind="file" size={22} />} title={form.name}
      subtitle={<>{form.published_version ? `Published version ${form.published_version}` : "Not published yet"}{form.has_draft && " · draft has changes"}{dirty && " · unsaved"}</>}
      actions={<div className="row" style={{ gap: 8 }}>
        <button className="btn secondary" onClick={() => { setPreview(!preview); setAnswers({}); }}>{preview ? "Edit" : "Preview"}</button>
        {admin && <button className="btn secondary" disabled={!dirty} onClick={() => void save().catch((e: Error) => setError(e.message))}>Save draft</button>}
        {admin && <button className="btn" disabled={!dirty && !form.has_draft} onClick={publish}>Publish</button>}</div>} />
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="scope-note" role="status">{message}</p>}
    {preview ? <section className="card"><FormRenderer definition={definition} answers={answers} onChange={setAnswers} /></section> : <>
      <div className="arg-grid">
        <div className="field"><label htmlFor="form-name">Name</label><input id="form-name" value={name} disabled={!admin} maxLength={120} onChange={(e) => { setName(e.target.value); setDirty(true); }} /></div>
        <div className="field"><label htmlFor="form-description">Description</label><input id="form-description" value={description} disabled={!admin} maxLength={1000}
          onChange={(e) => { setDescription(e.target.value); setDirty(true); }} /></div>
      </div>
      <FormBuilder definition={definition} onChange={change} readOnly={!admin} />
    </>}
    <h2>Versions</h2>
    <div className="table-scroll"><table aria-label="Versions"><thead><tr><th>Version</th><th>Status</th><th>Published</th></tr></thead><tbody>
      {[...versions].reverse().map((v) => <tr key={v.id}><td>{v.version}</td><td>{v.published ? (v.version === form.published_version ? "Current" : "Earlier") : "Draft"}</td>
        <td>{v.published_at ? `${new Date(v.published_at).toLocaleString()} by ${v.published_by}` : <span className="muted">—</span>}</td></tr>)}</tbody></table></div>
    {admin && <div className="form-actions"><button className="btn danger" disabled={form.used_by > 0} title={form.used_by ? "A policy uses this form" : undefined} onClick={remove}>Delete form</button></div>}
  </>;
}

/** Earlier questions a page or question can depend on: choices and statements. */
function ConditionEditor({ condition, earlier, onChange, readOnly, label }: { condition: FormCondition | null; earlier: FormField[];
  onChange: (c: FormCondition | null) => void; readOnly: boolean; label: string }) {
  const usable = earlier.filter((f) => ["choice", "multi_choice", "statement"].includes(f.type));
  const field = usable.find((f) => f.id === condition?.field);
  const valueOf = (f: FormField): FormCondition => f.type === "statement" ? { field: f.id, equals: true }
    : f.type === "multi_choice" ? { field: f.id, contains: f.options[0] ?? "" } : { field: f.id, equals: f.options[0] ?? "" };
  if (!usable.length) return null;
  return <div className="row condition-editor" style={{ gap: 6, flexWrap: "wrap" }}>
    <span className="muted small">Show</span>
    <select aria-label={`${label}: shown when`} value={condition?.field ?? ""} disabled={readOnly}
      onChange={(e) => { const f = usable.find((x) => x.id === e.target.value); onChange(f ? valueOf(f) : null); }}>
      <option value="">always</option>{usable.map((f) => <option key={f.id} value={f.id}>when “{f.label}”</option>)}</select>
    {field && field.type !== "statement" && <select aria-label={`${label}: answer`} value={String(condition?.contains ?? condition?.equals ?? "")} disabled={readOnly}
      onChange={(e) => onChange(field.type === "multi_choice" ? { field: field.id, contains: e.target.value } : { field: field.id, equals: e.target.value })}>
      {field.options.map((o) => <option key={o} value={o}>{field.type === "multi_choice" ? "includes" : "is"} {o}</option>)}</select>}
    {field?.type === "statement" && <span className="muted small">is agreed to</span>}
  </div>;
}

export function FormBuilder({ definition, onChange, readOnly = false }: { definition: FormDefinition; onChange: (d: FormDefinition) => void; readOnly?: boolean }) {
  const pages = definition.pages;
  const setPages = (next: FormPage[]) => onChange({ pages: next });
  const setPage = (i: number, patch: Partial<FormPage>) => setPages(pages.map((p, j) => j === i ? { ...p, ...patch } : p));
  const move = <T,>(list: T[], i: number, by: number) => { const next = [...list]; [next[i], next[i + by]] = [next[i + by], next[i]]; return next; };
  const setField = (pi: number, fi: number, patch: Partial<FormField>) => setPage(pi, { fields: pages[pi].fields.map((f, j) => j === fi ? { ...f, ...patch } : f) });
  const addField = (pi: number) => setPage(pi, { fields: [...pages[pi].fields,
    { id: freeId("q", allFields(definition).map((f) => f.id)), label: "", help: "", type: "short_text", required: true, options: [], show_if: null }] });
  // A question can only depend on questions before it; removing one clears conditions that pointed at it.
  const removeField = (pi: number, fi: number) => { const gone = pages[pi].fields[fi].id;
    const clear = (c: FormCondition | null) => c?.field === gone ? null : c;
    setPages(pages.map((p, j) => ({ ...p, show_if: clear(p.show_if), fields: p.fields.filter((_, k) => !(j === pi && k === fi)).map((f) => ({ ...f, show_if: clear(f.show_if) })) }))); };
  return <div className="form-builder" role="group" aria-label="Form builder">
    {pages.map((page, pi) => { const before = pages.slice(0, pi).flatMap((p) => p.fields);
      return <section key={page.id} className="card builder-page" aria-label={`Page ${pi + 1}`}>
        <div className="row" style={{ gap: 8 }}>
          <strong>Page {pi + 1}</strong>
          <input aria-label={`Page ${pi + 1} title`} value={page.title} disabled={readOnly} style={{ flex: 1 }} onChange={(e) => setPage(pi, { title: e.target.value })} />
          {!readOnly && <>
            <button type="button" className="icon-btn" aria-label={`Move page ${pi + 1} up`} disabled={pi === 0} onClick={() => setPages(move(pages, pi, -1))}>↑</button>
            <button type="button" className="icon-btn" aria-label={`Move page ${pi + 1} down`} disabled={pi === pages.length - 1} onClick={() => setPages(move(pages, pi, 1))}>↓</button>
            <button type="button" className="icon-btn" aria-label={`Remove page ${pi + 1}`} disabled={pages.length === 1} onClick={() => setPages(pages.filter((_, j) => j !== pi))}><IconTrash size={14} /></button></>}
        </div>
        <textarea aria-label={`Page ${pi + 1} introduction`} rows={2} value={page.intro} disabled={readOnly} placeholder="Introduction (optional)" onChange={(e) => setPage(pi, { intro: e.target.value })} />
        <ConditionEditor label={`Page ${pi + 1}`} condition={page.show_if} earlier={before} readOnly={readOnly} onChange={(c) => setPage(pi, { show_if: c })} />
        {page.fields.map((f, fi) => <div key={f.id} className="builder-field" role="group" aria-label={`Question ${f.label || f.id}`}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <input aria-label="Question" value={f.label} disabled={readOnly} placeholder="Question" style={{ flex: 2, minWidth: 200 }} onChange={(e) => setField(pi, fi, { label: e.target.value })} />
            <select aria-label="Answer type" value={f.type} disabled={readOnly} onChange={(e) => setField(pi, fi, { type: e.target.value as FieldType })}>
              {TYPES.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}</select>
            <label className="row"><input type="checkbox" checked={f.required} disabled={readOnly} onChange={(e) => setField(pi, fi, { required: e.target.checked })} />Required</label>
            {!readOnly && <>
              <button type="button" className="icon-btn" aria-label={`Move ${f.label || f.id} up`} disabled={fi === 0} onClick={() => setPage(pi, { fields: move(page.fields, fi, -1) })}>↑</button>
              <button type="button" className="icon-btn" aria-label={`Move ${f.label || f.id} down`} disabled={fi === page.fields.length - 1} onClick={() => setPage(pi, { fields: move(page.fields, fi, 1) })}>↓</button>
              <button type="button" className="icon-btn" aria-label={`Remove ${f.label || f.id}`} onClick={() => removeField(pi, fi)}><IconTrash size={14} /></button></>}
          </div>
          <input aria-label="Help text" value={f.help} disabled={readOnly} placeholder="Help text (optional)" onChange={(e) => setField(pi, fi, { help: e.target.value })} />
          {(f.type === "choice" || f.type === "multi_choice") && <textarea aria-label="Choices, one per line" rows={3} disabled={readOnly} placeholder="One choice per line"
            value={f.options.join("\n")} onChange={(e) => setField(pi, fi, { options: e.target.value.split("\n") })}
            onBlur={() => setField(pi, fi, { options: f.options.map((o) => o.trim()).filter(Boolean) })} />}
          <ConditionEditor label={f.label || f.id} condition={f.show_if} earlier={[...before, ...page.fields.slice(0, fi)]} readOnly={readOnly} onChange={(c) => setField(pi, fi, { show_if: c })} />
        </div>)}
        {!readOnly && <button type="button" className="btn sm secondary" onClick={() => addField(pi)}><IconPlus size={14} />Add question</button>}
      </section>; })}
    {!readOnly && <button type="button" className="btn secondary" onClick={() => setPages([...pages, newPage(pages)])}><IconPlus size={14} />Add page</button>}
  </div>;
}

const scopeText = (s: ComplianceRoleItem["scope"], layers: { id: string; name: string }[], groups: AppGroup[]) => [
  s.layers?.length ? `layers: ${s.layers.map((id) => layers.find((l) => l.id === id)?.name ?? id).join(", ")}` : "",
  s.classifications?.length ? `classifications: ${s.classifications.join(", ")}` : "",
  s.resource_ids?.length ? `${s.resource_ids.length} object${s.resource_ids.length === 1 ? "" : "s"}` : "",
  s.group_ids?.length ? `groups: ${s.group_ids.map((id) => groups.find((g) => g.id === id)?.name ?? id).join(", ")}` : "",
].filter(Boolean).join(" · ") || "Everything";

/** Who reviews, who approves new members, and who sets this up: roles given to groups, scoped. */
export function PrivacyRoles({ hub }: { hub: Hub }) {
  const admin = isComplianceAdmin(hub);
  const [data, setData] = useState<{ roles: ComplianceRoleItem[]; kinds: { key: string; label: string }[] } | null>(null);
  const [groups, setGroups] = useState<AppGroup[]>([]), [layers, setLayers] = useState<{ id: string; name: string }[]>([]), [levels, setLevels] = useState<ClassificationLevel[]>([]);
  const [error, setError] = useState(""), [role, setRole] = useState("compliance_reviewer"), [group, setGroup] = useState("");
  const [scope, setScope] = useState<Required<ComplianceRoleItem["scope"]>>({ resource_ids: [], layers: [], classifications: [], group_ids: [] });
  const load = useCallback(() => api.compliance.roles().then(setData).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void load(); api.access.groups().then(setGroups).catch(() => setGroups([]));
    api.compliance.policies().then((g) => { setLayers(g.layers); setLevels(g.classifications); }).catch(() => undefined); }, [load]);
  if (!data) return error ? <p className="error" role="alert">{error}</p> : <p className="muted" role="status">Loading…</p>;
  const toggle = (key: keyof typeof scope, value: string) => setScope({ ...scope, [key]: scope[key].includes(value) ? scope[key].filter((v) => v !== value) : [...scope[key], value] });
  const add = (e: React.FormEvent) => { e.preventDefault(); setError("");
    api.compliance.addRole({ role, app_group_id: group, scope }).then(() => { setGroup(""); setScope({ resource_ids: [], layers: [], classifications: [], group_ids: [] }); void load(); })
      .catch((err: Error) => setError(err.message)); };
  const checks = (key: keyof typeof scope, items: { id: string; name: string }[], legend: string) => <fieldset className="field form-choices"><legend>{legend}</legend>
    {items.map((i) => <label key={i.id} className="row"><input type="checkbox" checked={scope[key].includes(i.id)} onChange={() => toggle(key, i.id)} />{i.name}</label>)}
    <span className="hint">None chosen: all.</span></fieldset>;
  return <>
    <PageHeader icon={<ProductIcon kind="groups" size={22} />} title="Compliance roles"
      subtitle="Give app groups a compliance job: reviewing forms, approving people who join groups with justified access, or setting this up. Scope a role to layers, classifications, objects or groups." />
    {error && <p className="error" role="alert">{error}</p>}
    <div className="table-scroll"><table aria-label="Compliance roles"><thead><tr><th>Role</th><th>Group</th><th>Scope</th>{admin && <th />}</tr></thead><tbody>
      {data.roles.map((r) => <tr key={r.id}><td>{r.label}</td><td>{r.group}</td><td>{scopeText(r.scope, layers, groups)}</td>
        {admin && <td><button className="icon-btn" aria-label={`Remove ${r.label} from ${r.group}`} onClick={() => { setError("");
          api.compliance.removeRole(r.id).then(() => void load()).catch((err: Error) => setError(err.message)); }}><IconTrash size={14} /></button></td>}</tr>)}
      {!data.roles.length && <tr><td colSpan={admin ? 4 : 3} className="muted">No compliance roles yet. Platform administrators can do everything.</td></tr>}</tbody></table></div>
    {admin && <form className="card" aria-label="Give a compliance role" onSubmit={add}>
      <h2>Give a role</h2>
      <div className="arg-grid">
        <div className="field"><label htmlFor="role-kind">Role</label><select id="role-kind" value={role} onChange={(e) => setRole(e.target.value)}>
          {data.kinds.filter((k) => hub.me?.is_admin || k.key !== "compliance_admin").map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}</select></div>
        <div className="field"><label htmlFor="role-group">To group</label><select id="role-group" value={group} onChange={(e) => setGroup(e.target.value)}>
          <option value="">Choose a group</option>{groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}</select></div>
      </div>
      {role === "compliance_reviewer" && <div className="arg-grid">
        {checks("layers", layers, "Layers")}{checks("classifications", levels.map((l) => ({ id: l.name, name: l.name })), "Classifications")}</div>}
      {role === "membership_approver" && checks("group_ids", groups.map((g) => ({ id: g.id, name: g.name })), "Groups")}
      <div className="form-actions"><button className="btn" disabled={!group}>Give role</button></div>
      <p className="hint">Scoping a reviewer to specific objects is possible with Terraform or the API (resource_ids). Also configurable with Terraform (fdp_compliance_role).</p>
    </form>}
  </>;
}

/** The review queue: what waits for you, oldest deadline first; or every case you may see. */
export function PrivacyReviews({ openTab }: { openTab: (t: TabRef) => void }) {
  const [box, setBox] = useState<"review" | "all">("review"), [status, setStatus] = useState("");
  const [cases, setCases] = useState<AccessCaseItem[] | null>(null), [error, setError] = useState("");
  useEffect(() => { setCases(null); api.compliance.cases(box, box === "all" ? status : "").then((list) => setCases(box === "review"
    ? [...list].sort((a, b) => (a.due_at ?? "9").localeCompare(b.due_at ?? "9")) : list)).catch((e: Error) => setError(e.message)); }, [box, status]);
  const late = (cases ?? []).filter(overdue).length;
  return <>
    <PageHeader icon={<ProductIcon kind="identity" size={22} />} title="Reviews"
      subtitle="Compliance forms waiting for your decision, with their deadlines. Open one to read the answers, ask questions and decide." />
    <nav className="object-tabs" aria-label="Review boxes">
      <button className={box === "review" ? "active" : ""} aria-current={box === "review" ? "page" : undefined} onClick={() => setBox("review")}>Waiting for me</button>
      <button className={box === "all" ? "active" : ""} aria-current={box === "all" ? "page" : undefined} onClick={() => setBox("all")}>All cases</button>
    </nav>
    {box === "all" && <div className="row" style={{ gap: 8, marginBottom: 8 }}><label htmlFor="case-status">Status</label>
      <select id="case-status" value={status} onChange={(e) => setStatus(e.target.value)}><option value="">Any</option>
        {["in_review", "changes_requested", "approved", "declined", "withdrawn", "expired"].map((s) => <option key={s} value={s}>{s.replace("_", " ")}</option>)}</select></div>}
    {error && <p className="error" role="alert">{error}</p>}
    {box === "review" && late > 0 && <p className="notice" role="status">{late} overdue.</p>}
    {!cases ? <p className="muted" role="status">Loading…</p> : <CaseTable cases={cases} openTab={openTab} label={box === "review" ? "Waiting for me" : "All cases"} />}
  </>;
}

/** People who joined a group with access given through a form: they get that access once approved. */
export function PrivacyMemberships({ hub }: { hub: Hub }) {
  const [items, setItems] = useState<{ app_group_id: string; principal_id: string; group: string; principal: string }[] | null>(null);
  const [error, setError] = useState(""), [message, setMessage] = useState(""), [notes, setNotes] = useState<Record<string, string>>({});
  const load = useCallback(() => api.compliance.memberships().then(setItems).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void load(); }, [load]);
  const decide = (g: string, p: string, name: string, approve: boolean) => { setError(""); setMessage("");
    api.compliance.decideMembership(g, p, approve, notes[g + p] ?? "").then(() => { setMessage(`${approve ? "Approved" : "Declined"} ${name}.`); void load(); void hub.refresh(); })
      .catch((e: Error) => setError(e.message)); };
  return <>
    <PageHeader icon={<ProductIcon kind="groups" size={22} />} title="Memberships"
      subtitle="People who joined a group that has access given through a compliance form. They have the group's other access already; this access waits for your approval." />
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="success-message" role="status">{message}</p>}
    {!items ? <p className="muted" role="status">Loading…</p> : <div className="table-scroll"><table aria-label="Memberships to approve"><thead><tr><th>Person</th><th>Group</th><th /></tr></thead><tbody>
      {items.map((m) => <tr key={m.app_group_id + m.principal_id}><td>{m.principal}</td><td>{m.group}</td>
        <td className="table-actions"><div className="row">
          <input aria-label={`Note for ${m.principal}`} placeholder="Note (optional)" value={notes[m.app_group_id + m.principal_id] ?? ""}
            onChange={(e) => setNotes({ ...notes, [m.app_group_id + m.principal_id]: e.target.value })} />
          <button className="btn sm" onClick={() => decide(m.app_group_id, m.principal_id, m.principal, true)}>Approve</button>
          <button className="btn sm secondary" onClick={() => decide(m.app_group_id, m.principal_id, m.principal, false)}>Decline</button></div></td></tr>)}
      {!items.length && <tr><td colSpan={3} className="muted">Nobody waits.</td></tr>}</tbody></table></div>}
  </>;
}

/** Access given through compliance forms: who has what, why, approved by whom and until when. */
export function PrivacyRegister({ openTab }: { openTab: (t: TabRef) => void }) {
  const [rows, setRows] = useState<RegisterRow[] | null>(null), [error, setError] = useState(""), [query, setQuery] = useState("");
  const [deadlines, setDeadlines] = useState<JustifyDeadline[]>([]);
  useEffect(() => { api.compliance.register().then(setRows).catch((e: Error) => setError(e.message));
    api.compliance.deadlines().then(setDeadlines).catch(() => setDeadlines([])); }, []);
  const soon = (v: string | null) => !!v && new Date(v).getTime() - Date.now() < 30 * 86400000;
  const shown = (rows ?? []).filter((r) => !query || JSON.stringify(r).toLowerCase().includes(query.toLowerCase()));
  const exportCsv = () => api.compliance.registerCsv().then((text) => saveFile(new Blob([text], { type: "text/csv" }), "access-register.csv")).catch((e: Error) => setError(e.message));
  const day = (v: string | null) => v ? new Date(v).toLocaleDateString() : "—";
  return <>
    <PageHeader icon={<ProductIcon kind="file" size={22} />} title="Access register"
      subtitle="Access given because a compliance form was approved. Reviewers see what is in their scope."
      actions={<button className="btn secondary" onClick={() => void exportCsv()}>Export CSV</button>} />
    {error && <p className="error" role="alert">{error}</p>}
    <input aria-label="Filter the register" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter by group, data, person…" style={{ marginBottom: 8, minWidth: 280 }} />
    {!rows ? <p className="muted" role="status">Loading…</p> : <div className="table-scroll"><table aria-label="Access register"><thead><tr>
      <th>Group</th><th>Access</th><th>Data</th><th>Form</th><th>Approved by</th><th>Approved</th><th>Until</th><th>People</th></tr></thead><tbody>
      {shown.map((r) => <tr key={r.case_id}><td>{r.group}</td>
        <td><button className="text-button" onClick={() => openTab({ kind: "privacy-case", ref: r.case_id, title: r.access })}>{r.access}</button></td>
        <td><span className="product-chip">{r.classification}</span>{r.layer && <span className="muted"> · {r.layer}</span>}</td>
        <td>{r.form}<div className="muted small">asked by {r.requester}</div></td><td>{r.approved_by}</td><td>{day(r.approved_at)}</td>
        <td className={soon(r.until) ? "error-text" : ""}>{day(r.until)}</td><td>{r.members}</td></tr>)}
      {!shown.length && <tr><td colSpan={8} className="muted">No access given through forms{query && " matches"}.</td></tr>}</tbody></table></div>}
    {!!deadlines.length && <><h2>Waiting for a form</h2>
      <p className="hint">Access that needs a compliance form and has none: it existed before the policy, or its data was classified higher since. It is removed at the deadline unless a form is approved.</p>
      <div className="table-scroll"><table aria-label="Waiting for a form"><thead><tr><th>Group</th><th>Access</th><th>Data</th><th>Deadline</th></tr></thead><tbody>
        {deadlines.map((d) => <tr key={d.resource_id + d.app_group_id}><td>{d.group}</td>
          <td>{d.product_id ? <button className="text-button" onClick={() => openTab({ kind: "product", ref: d.product_id!, title: d.resource })}>{d.resource}</button> : `${d.role} on ${d.resource}`}</td>
          <td><span className="product-chip">{d.classification}</span></td><td className={soon(d.due_at) ? "error-text" : ""}>{day(d.due_at)}</td></tr>)}</tbody></table></div></>}
  </>;
}
