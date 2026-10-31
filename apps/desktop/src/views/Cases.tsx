import { useCallback, useEffect, useState } from "react";
import { AccessCaseItem, ago, api, FormDefinition, saveFile } from "../api";
import type { Hub } from "../hub";
import { PageHeader } from "../components/Page";
import { ProductIcon } from "../components/ProductIcon";
import { Answers, FormRenderer, shown, uploadFiles, visiblePages } from "../components/ComplianceForm";
import type { TabRef } from "./Tabs";

const STATUS: Record<AccessCaseItem["status"], [string, string]> = {
  draft: ["Draft", "stopped"], in_review: ["In review", "queued"], changes_requested: ["Changes asked", "queued"],
  approved: ["Approved", "succeeded"], declined: ["Declined", "failed"], withdrawn: ["Withdrawn", "stopped"], expired: ["Expired", "stopped"] };
const STEP = { data_owner: "data owners", compliance_reviewer: "compliance reviewers" } as Record<string, string>;
const day = (value: string | null | undefined) => value ? new Date(value).toLocaleDateString() : "";

/** What a case gives, in a few words. */
export function caseWhat(c: AccessCaseItem) {
  if (c.kind === "product") return `${c.product ?? "Data product"}${c.option ? ` (${c.option})` : ""}`;
  if (c.kind === "membership") return `${c.principal ?? "Someone"} joining ${c.group}`;
  return `${c.role} on ${c.resource?.name ?? "an object"}`;
}

export const overdue = (c: AccessCaseItem) => c.status === "in_review" && !!c.due_at && new Date(c.due_at).getTime() < Date.now();

export function CaseStatus({ c }: { c: AccessCaseItem }) {
  const [label, dot] = STATUS[c.status];
  return <span className="status"><span className={`dot ${dot}`} />{label}
    {c.status === "in_review" && c.steps[c.step] && <span className="muted"> · {STEP[c.steps[c.step].role]}{c.steps.length > 1 ? ` (step ${c.step + 1} of ${c.steps.length})` : ""}</span>}</span>;
}

/** A list of cases; a row opens the case. */
export function CaseTable({ cases, openTab, label, tabKind = "privacy-case", requester = true }: { cases: AccessCaseItem[]; openTab: (t: TabRef) => void;
  label: string; tabKind?: "privacy-case" | "case"; requester?: boolean }) {
  return <div className="table-scroll"><table aria-label={label}><thead><tr><th>Access</th><th>For group</th><th>Data</th>{requester && <th>Requested by</th>}<th>Status</th><th>Due</th></tr></thead><tbody>
    {cases.map((c) => <tr key={c.id} className={overdue(c) ? "overdue" : ""}>
      <td><button className="text-button" onClick={() => openTab({ kind: tabKind, ref: c.id, title: caseWhat(c) })}>{caseWhat(c)}</button>
        <div className="muted small">{c.form ? `${c.form} v${c.form_version}` : c.kind === "membership" ? "Membership" : ""}</div></td>
      <td>{c.group}</td>
      <td>{c.classification && <span className="product-chip">{c.classification}</span>}{c.layer && <span className="muted"> · {c.layer}</span>}</td>
      {requester && <td>{c.requester} <span className="muted" title={new Date(c.created_at).toLocaleString()}>{ago(c.submitted_at ?? c.created_at)}</span></td>}
      <td><CaseStatus c={c} /></td>
      <td>{c.status === "in_review" && c.due_at ? <span className={overdue(c) ? "error-text" : ""}>{day(c.due_at)}{overdue(c) && " · overdue"}</span>
        : c.approved_until ? <span className="muted">until {day(c.approved_until)}</span> : <span className="muted">—</span>}</td>
    </tr>)}
    {!cases.length && <tr><td colSpan={requester ? 6 : 5} className="muted">Nothing here.</td></tr>}</tbody></table></div>;
}

/** The answers as they were given, page by page; hidden pages and questions are left out. */
function FormAnswers({ definition, answers, files, onFile }: { definition: FormDefinition; answers: Answers;
  files: NonNullable<AccessCaseItem["files"]>; onFile: (id: string, name: string) => void }) {
  const show = (v: unknown) => v === true ? "Yes, agreed" : v === false ? "No" : Array.isArray(v) ? v.join(", ") : v === undefined || v === "" ? "—" : String(v);
  return <div className="form-answers" aria-label="Answers">
    {visiblePages(definition, answers).map((page) => <section key={page.id}>
      <h3>{page.title}</h3>
      <dl>{page.fields.filter((f) => shown(f.show_if, answers)).map((f) => { const attached = files.filter((x) => x.field_id === f.id);
        return <div key={f.id}><dt>{f.label}</dt>
          <dd>{f.type !== "file" ? show(answers[f.id]) : attached.length ? attached.map((x) => <button key={x.id} className="text-button" onClick={() => onFile(x.id, x.name)}>{x.name}</button>) : "—"}</dd></div>; })}</dl>
    </section>)}
  </div>;
}

export function CaseView({ id, hub, openTab }: { id: string; hub: Hub; openTab: (t: TabRef) => void }) {
  const [c, setCase] = useState<AccessCaseItem | null>(null), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [note, setNote] = useState(""), [until, setUntil] = useState(""), [comment, setComment] = useState(""), [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false), [answers, setAnswers] = useState<Answers>({}), [files, setFiles] = useState<Record<string, File>>({}), [complete, setComplete] = useState(false);
  const load = useCallback(() => api.compliance.case(id).then((x) => { setCase(x); setAnswers(x.answers ?? {}); }).catch((e: Error) => setError(e.message)), [id]);
  useEffect(() => { void load(); }, [load]);
  if (!c) return error ? <p className="error" role="alert">{error}</p> : <p className="muted" role="status">Loading…</p>;
  const act = (p: Promise<unknown>, text: string) => { setBusy(true); setError(""); setMessage("");
    return p.then(() => { setMessage(text); setNote(""); setComment(""); setEditing(false); void load(); void hub.refresh(); })
      .catch((e: Error) => setError(e.message)).finally(() => setBusy(false)); };
  const decide = (decision: "approve" | "changes" | "decline") => act(api.compliance.decide(c.id, decision, note.trim(), until || null),
    decision === "approve" ? "Approved." : decision === "changes" ? "Sent back to the requester." : "Declined.");
  const resubmit = () => act((async () => { await api.compliance.updateCase(c.id, { answers });
    if (Object.keys(files).length) await uploadFiles(c.id, files); await api.compliance.submitCase(c.id); })(), c.status === "draft" ? "Sent for review." : "Sent for review again.");
  const caseTab = (ref: string, title: string) => openTab({ kind: "privacy-case", ref, title });
  const renew = () => { setBusy(true); setError("");
    api.compliance.renew(c.id).then((r) => caseTab(r.id, caseWhat(r))).catch((e: Error) => setError(e.message)).finally(() => setBusy(false)); };
  const download = (fileId: string, name: string) => void api.compliance.caseFile(c.id, fileId).then((b) => saveFile(b, name)).catch((e: Error) => setError(e.message));
  const history = [...(c.submitted_at ? [{ at: c.submitted_at, by: c.requester, text: "sent the form" }] : []),
    ...c.decisions.map((d) => ({ at: d.at, by: d.by, text: `${d.decision === "approve" ? "approved" : d.decision === "changes" ? "asked for changes" : "declined"} as ${STEP[d.role] ?? d.role}${d.note ? `: ${d.note}` : ""}` })),
    ...(c.comments ?? []).map((x) => ({ at: x.at, by: x.by, text: `commented: ${x.text}` }))].sort((a, b) => a.at.localeCompare(b.at));
  const open = ["draft", "in_review", "changes_requested"].includes(c.status);
  return <>
    <PageHeader icon={<ProductIcon kind="identity" size={22} />} title={caseWhat(c)}
      subtitle={<><CaseStatus c={c} /> · for {c.group} · asked by {c.requester}{c.due_at && c.status === "in_review" && <> · review due {day(c.due_at)}{overdue(c) && <strong className="error-text"> (overdue)</strong>}</>}</>} />
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="success-message" role="status">{message}</p>}
    <section className="card case-facts" aria-label="What it gives">
      <dl className="facts">
        <div><dt>Gives</dt><dd>{c.kind === "product" ? <>Consumer of <button className="text-button" onClick={() => openTab({ kind: "product", ref: c.product_id!, title: c.product ?? "Product" })}>{c.product}</button>{c.option && ` with option ${c.option}`}</>
          : c.kind === "role" ? `${c.role} on ${c.resource?.kind ?? ""} ${c.resource?.name ?? ""}` : `the group's justified access to ${c.principal}`}</dd></div>
        <div><dt>Data</dt><dd>{c.classification || "—"}{c.layer && ` in ${c.layer}`}</dd></div>
        <div><dt>Form</dt><dd>{c.form ? `${c.form}, version ${c.form_version}` : "—"}</dd></div>
        <div><dt>Asked until</dt><dd>{day(c.until) || "No end asked"}{c.approved_until && <> · approved until <strong>{day(c.approved_until)}</strong></>}</dd></div>
        {!!c.members?.length && <div><dt>People in {c.group}</dt><dd>{c.members.join(", ")}</dd></div>}
        <div><dt>Review</dt><dd>{c.steps.map((s, i) => `${i + 1}. ${STEP[s.role]}${s.approvals > 1 ? ` (${s.approvals} approvals)` : ""}`).join(" → ") || "—"}</dd></div>
      </dl>
    </section>
    {c.definition && (editing ? <section className="card"><FormRenderer definition={c.definition} answers={answers} onChange={setAnswers} files={files} onComplete={setComplete}
        onFile={(f, file) => setFiles((all) => { const next = { ...all }; if (file) next[f] = file; else delete next[f]; return next; })} />
        <div className="form-actions"><button className="btn" disabled={busy || !complete} onClick={() => void resubmit()}>{c.status === "draft" ? "Send for review" : "Send again"}</button>
          <button className="btn secondary" onClick={() => { setEditing(false); setAnswers(c.answers ?? {}); }}>Cancel</button></div></section>
      : <FormAnswers definition={c.definition} answers={c.answers ?? {}} files={c.files ?? []} onFile={download} />)}
    {c.can_review && <section className="card decision-panel" aria-label="Decide">
      <h2>Your decision</h2>
      <div className="field"><label htmlFor="decision-note">Note to the requester</label>
        <textarea id="decision-note" rows={3} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Needed when you ask for changes or decline" /></div>
      <div className="field"><label htmlFor="decision-until">Approve until (optional)</label>
        <input id="decision-until" type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        <span className="hint">Shorter than asked if you choose an earlier date; never longer than the policy allows.</span></div>
      <div className="form-actions">
        <button className="btn" disabled={busy} onClick={() => void decide("approve")}>Approve</button>
        <button className="btn secondary" disabled={busy || !note.trim()} onClick={() => void decide("changes")}>Ask for changes</button>
        <button className="btn danger" disabled={busy || !note.trim()} onClick={() => void decide("decline")}>Decline</button></div>
    </section>}
    {c.renewal_of && <p className="hint">Renews <button className="text-button" onClick={() => caseTab(c.renewal_of!, "Earlier case")}>the earlier case</button>; the answers were filled in from it.</p>}
    {(c.status === "approved" || c.status === "expired") && c.kind !== "membership" && <div className="form-actions">
      {c.renewal ? <button className="btn secondary" onClick={() => caseTab(c.renewal!, "Renewal")}>Open the renewal</button>
        : <button className="btn secondary" disabled={busy} onClick={renew}>Renew</button>}
      {c.status === "approved" && c.approved_until && <span className="muted">Access ends {day(c.approved_until)}.</span>}</div>}
    {c.is_requester && open && !editing && <div className="form-actions">
      {c.status === "changes_requested" && <button className="btn" onClick={() => setEditing(true)}>Change answers</button>}
      {c.status === "draft" && <button className="btn" onClick={() => setEditing(true)}>Check answers and send</button>}
      <button className="btn secondary" disabled={busy} onClick={() => void act(api.compliance.withdraw(c.id), "Withdrawn.")}>Withdraw</button></div>}
    <h2>History</h2>
    <ol className="case-history">{history.map((h, i) => <li key={i}><span className="muted" title={new Date(h.at).toLocaleString()}>{new Date(h.at).toLocaleString()}</span> <strong>{h.by}</strong> {h.text}</li>)}
      {!history.length && <li className="muted">Not sent yet.</li>}</ol>
    <form className="row" style={{ gap: 8 }} onSubmit={(e) => { e.preventDefault(); void act(api.compliance.comment(c.id, comment.trim()), "Comment added."); }}>
      <input aria-label="Comment" value={comment} maxLength={4000} onChange={(e) => setComment(e.target.value)} placeholder="Ask or answer a question" style={{ flex: 1 }} />
      <button className="btn secondary" disabled={busy || !comment.trim()}>Comment</button></form>
  </>;
}
