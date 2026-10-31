import { useEffect, useState } from "react";
import { api, CaseTarget, FormCondition, FormDefinition, FormField, Requirement } from "../api";

export type Answers = Record<string, unknown>;

function shown(condition: FormCondition | null, answers: Answers): boolean {
  if (!condition) return true;
  const value = answers[condition.field];
  if ("equals" in condition) return value === condition.equals;
  return Array.isArray(value) && value.includes(condition.contains);
}

export function visiblePages(definition: FormDefinition, answers: Answers) {
  return definition.pages.filter((p) => shown(p.show_if, answers));
}

/** What is still missing on a page: required questions without an answer (files count when chosen). */
function missing(fields: FormField[], answers: Answers, files: Record<string, File>) {
  return fields.filter((f) => f.required && shown(f.show_if, answers) && (f.type === "file" ? !files[f.id]
    : f.type === "statement" ? answers[f.id] !== true : answers[f.id] === undefined || answers[f.id] === "" || (Array.isArray(answers[f.id]) && !(answers[f.id] as unknown[]).length)));
}

function Field({ field, value, onChange, file, onFile, readOnly }: { field: FormField; value: unknown; onChange: (v: unknown) => void;
  file?: File; onFile: (f: File | undefined) => void; readOnly: boolean }) {
  const id = `form-${field.id}`;
  const label = <label htmlFor={id}>{field.label}{field.required && <span className="muted"> (required)</span>}</label>;
  const help = field.help && <span className="hint">{field.help}</span>;
  switch (field.type) {
    case "long_text": return <div className="field">{label}<textarea id={id} rows={4} disabled={readOnly} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />{help}</div>;
    case "choice": return <fieldset className="field form-choices"><legend>{field.label}{field.required && <span className="muted"> (required)</span>}</legend>
      {field.options.map((o) => <label key={o} className="row"><input type="radio" name={id} disabled={readOnly} checked={value === o} onChange={() => onChange(o)} />{o}</label>)}{help}</fieldset>;
    case "multi_choice": { const list = Array.isArray(value) ? value as string[] : [];
      return <fieldset className="field form-choices"><legend>{field.label}{field.required && <span className="muted"> (required)</span>}</legend>
        {field.options.map((o) => <label key={o} className="row"><input type="checkbox" disabled={readOnly} checked={list.includes(o)}
          onChange={(e) => onChange(e.target.checked ? [...list, o] : list.filter((x) => x !== o))} />{o}</label>)}{help}</fieldset>; }
    case "date": return <div className="field">{label}<input id={id} type="date" disabled={readOnly} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />{help}</div>;
    case "number": return <div className="field">{label}<input id={id} type="number" disabled={readOnly} value={value === undefined ? "" : String(value)} onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))} />{help}</div>;
    case "statement": return <div className="field"><label className="row form-statement"><input id={id} type="checkbox" disabled={readOnly} checked={value === true} onChange={(e) => onChange(e.target.checked)} />{field.label}</label>{help}</div>;
    case "file": return <div className="field">{label}{readOnly ? <span className="muted">{value ? "Attached" : "No file"}</span>
      : <input id={id} type="file" onChange={(e) => onFile(e.target.files?.[0])} />}{file && <span className="hint">{file.name}</span>}{help}</div>;
    default: return <div className="field">{label}<input id={id} disabled={readOnly} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />{help}</div>;
  }
}

/** A compliance form, page by page: Next and Back, where you are, and what is still missing. */
export function FormRenderer({ definition, answers, onChange, files = {}, onFile = () => undefined, readOnly = false, onComplete }: {
  definition: FormDefinition; answers: Answers; onChange: (a: Answers) => void; files?: Record<string, File>;
  onFile?: (field: string, f: File | undefined) => void; readOnly?: boolean; onComplete?: (complete: boolean) => void;
}) {
  const pages = visiblePages(definition, answers);
  const [index, setIndex] = useState(0);
  const page = pages[Math.min(index, pages.length - 1)];
  const complete = pages.every((p) => !missing(p.fields, answers, files).length);
  useEffect(() => { onComplete?.(complete); }, [complete]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!page) return null;
  const gaps = missing(page.fields, answers, files);
  return <div className="compliance-form" role="group" aria-label="Compliance form">
    {pages.length > 1 && <ol className="form-steps" aria-label="Pages">{pages.map((p, i) => <li key={p.id} className={i === index ? "current" : ""}>
      <button type="button" className="text-button" onClick={() => setIndex(i)} aria-current={i === index ? "step" : undefined}>{i + 1}. {p.title}</button></li>)}</ol>}
    <h3>{page.title}</h3>
    {page.intro && <p className="muted">{page.intro}</p>}
    {page.fields.filter((f) => shown(f.show_if, answers)).map((f) => <Field key={f.id} field={f} value={answers[f.id]} readOnly={readOnly}
      onChange={(v) => onChange({ ...answers, [f.id]: v })} file={files[f.id]} onFile={(file) => onFile(f.id, file)} />)}
    {pages.length > 1 && <div className="row" style={{ gap: 8 }}>
      <button type="button" className="btn sm secondary" disabled={index === 0} onClick={() => setIndex(index - 1)}>Back</button>
      <span className="muted">Page {index + 1} of {pages.length}</span>
      <button type="button" className="btn sm secondary" disabled={index >= pages.length - 1 || (!readOnly && gaps.length > 0)} onClick={() => setIndex(index + 1)}>Next</button>
      {!readOnly && gaps.length > 0 && <span className="hint">Still to answer: {gaps.map((g) => g.label).join(", ")}</span>}</div>}
  </div>;
}

export async function uploadFiles(caseId: string, files: Record<string, File>) {
  for (const [field, file] of Object.entries(files)) {
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
    await api.compliance.addFile(caseId, field, file.name, file.type || "application/octet-stream", data);
  }
}

/** When access needs a compliance form: fill it in; the access is given once the review approves it. */
export function JustifyPanel({ target, requirement, onDone, onCancel }: { target: CaseTarget; requirement: Requirement; onDone: (message: string) => void; onCancel: () => void }) {
  const [answers, setAnswers] = useState<Answers>({}), [files, setFiles] = useState<Record<string, File>>({});
  const [until, setUntil] = useState(""), [complete, setComplete] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const definition = requirement.form?.definition;
  if (!definition) return <p className="error" role="alert">The policy's form has no published version yet. Ask the privacy team.</p>;
  const send = async () => {
    setBusy(true); setError("");
    try {
      const hasFiles = Object.keys(files).length > 0;
      let item = await api.compliance.createCase({ ...target, answers, until: until || null, submit: !hasFiles });
      if (hasFiles) { await uploadFiles(item.id, files); item = await api.compliance.submitCase(item.id); }
      onDone(`Sent for review (case ${item.id}). The access is given once it is approved.`);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return <section className="card justify-panel" aria-label="Justify access">
    <h2>{requirement.form?.name}</h2>
    <p className="muted">This data is <strong>{requirement.classification}</strong> in this layer, so access needs this form and a review
      ({(requirement.steps ?? []).map((s) => s.role === "data_owner" ? "data owners" : "compliance reviewers").join(", then ")}).
      Approved access lasts at most {requirement.max_days} days; reviews are due within {requirement.review_days} days.</p>
    <FormRenderer definition={definition} answers={answers} onChange={setAnswers} files={files}
      onFile={(f, file) => setFiles((all) => { const next = { ...all }; if (file) next[f] = file; else delete next[f]; return next; })} onComplete={setComplete} />
    <div className="field"><label htmlFor="justify-until">Access needed until (optional)</label><input id="justify-until" type="date" value={until} onChange={(e) => setUntil(e.target.value)} /></div>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="form-actions"><button className="btn" disabled={busy || !complete} onClick={() => void send()}>{busy ? "Sending…" : "Send for review"}</button>
      <button className="btn secondary" onClick={onCancel}>Cancel</button></div>
  </section>;
}
