import { useCallback, useEffect, useState } from "react";
import { api, GitCredential, GitCredentialInput, usesGit, Workspace } from "../api";
import { PageHeader } from "../components/Page";
import { IconKey, IconPlus, IconTrash } from "../components/Icons";

const blank = (): GitCredentialInput => ({ label: "", host: "github.com", username: "", token: "", commit_name: "", commit_email: "" });

/** Fields of one credential; the token is required when adding, optional (keep) when editing. */
function CredentialFields({ value, onChange, editing, idPrefix }: { value: GitCredentialInput; onChange: (v: GitCredentialInput) => void; editing: boolean; idPrefix: string }) {
  const field = (key: keyof GitCredentialInput, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) =>
    <div className="field"><label htmlFor={`${idPrefix}-${key}`}>{label}</label>
      <input id={`${idPrefix}-${key}`} autoComplete="off" value={value[key] ?? ""} onChange={(e) => onChange({ ...value, [key]: e.target.value })} {...props} /></div>;
  return <>
    <div className="arg-grid">
      {field("label", "Name", { placeholder: "GitHub work" })}
      {field("host", "Host", { required: true, placeholder: "github.com" })}
      {field("username", "Username", { required: true })}
      {field("token", editing ? "Token (empty keeps the saved one)" : "Token", { type: "password", required: !editing, autoComplete: "new-password" })}
      {field("commit_name", "Commit as (name)", { placeholder: "Your name" })}
      {field("commit_email", "Commit as (email)", { type: "email", placeholder: "you@example.com" })}
    </div>
    <p className="muted">GitHub: a fine-grained token for the repositories you work on, with Contents read and write; as email, your GitHub noreply address (Settings → Emails). The token is stored in the platform's secret store and never shown again.</p>
  </>;
}

const valid = (v: GitCredentialInput, editing: boolean) => !!v.host.trim() && !!v.username.trim() && (editing || !!v.token);

/** Your own Git credentials: git in your VS Code and RStudio workspaces gets the token from the platform. */
export function GitCredentialsTab() {
  const [rows, setRows] = useState<GitCredential[]>([]), [error, setError] = useState("");
  const [draft, setDraft] = useState<(GitCredentialInput & { id?: string }) | null>(null), [busy, setBusy] = useState(false);
  const reload = useCallback(() => api.gitCredentials.list().then(setRows).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void reload(); }, [reload]);
  const save = async (e: React.FormEvent) => {
    e.preventDefault(); if (!draft) return; setBusy(true); setError("");
    const { id, ...body } = draft;
    try { if (id) await api.gitCredentials.update(id, body); else await api.gitCredentials.create(body); setDraft(null); await reload(); }
    catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  const remove = async (row: GitCredential) => {
    if (!confirm(`Remove ${row.label}? Workspaces using it ask for a credential at their next start.`)) return;
    try { await api.gitCredentials.remove(row.id); await reload(); } catch (err) { setError((err as Error).message); }
  };
  return <>
    <PageHeader icon={<IconKey size={22} />} title="Git credentials"
      subtitle="Your own Git tokens, for example one for work and one for personal repositories. Each VS Code or RStudio workspace uses the one you choose for it (asked at its first start): git gets the token from the platform when it needs it, so it is never saved in a workspace, and your commits carry the name and email set here. Deployments read repositories with the administrators' read-only credentials, never yours."
      actions={<button className="btn" onClick={() => setDraft(blank())}><IconPlus size={14} />Add credential</button>} />
    {error && <p className="error" role="alert">{error}</p>}
    {draft && <form className="card" aria-label="Git credential" onSubmit={(e) => void save(e)}>
      <CredentialFields value={draft} onChange={(v) => setDraft({ ...draft, ...v })} editing={!!draft.id} idPrefix="gc" />
      <div className="form-actions"><button className="btn" disabled={busy || !valid(draft, !!draft.id)}>Save credential</button>
        <button type="button" className="btn secondary" onClick={() => setDraft(null)}>Cancel</button></div>
    </form>}
    {!rows.length ? <p className="muted">No credentials yet. Public repositories clone without one.</p>
    : <table aria-label="Git credentials"><thead><tr><th>Name</th><th>Host</th><th>Username</th><th>Commits as</th><th /></tr></thead><tbody>
      {rows.map((r) => <tr key={r.id}><td><strong>{r.label}</strong></td><td className="mono">{r.host}</td><td>{r.username}</td>
        <td>{r.commit_name || r.commit_email ? <>{r.commit_name}{r.commit_email && <span className="muted"> &lt;{r.commit_email}&gt;</span>}</> : <span className="muted">your platform name</span>}</td>
        <td className="table-actions"><div className="row">
          <button className="btn sm secondary" onClick={() => setDraft({ id: r.id, label: r.label, host: r.host, username: r.username, token: "", commit_name: r.commit_name, commit_email: r.commit_email })}>Edit</button>
          <button className="icon-btn" aria-label={`Remove ${r.label}`} onClick={() => void remove(r)}><IconTrash size={14} /></button></div></td></tr>)}
    </tbody></table>}
  </>;
}

// --- choosing a credential for a workspace ----------------------------------------------------------------

type Pending = { ws: Workspace; resolve: (go: boolean) => void };
let ask: ((p: Pending) => void) | null = null;

/** Before a VS Code or RStudio workspace starts for the first time: which Git credential it uses. Resolves
 * false when the person cancels (do not start). Workspaces that already have a choice pass straight through. */
export function ensureGitChoice(ws: Workspace): Promise<boolean> {
  if (!usesGit(ws) || ws.git_asked || !ask) return Promise.resolve(true);
  return new Promise((resolve) => ask!({ ws, resolve }));
}

/** Mounted once in the app shell; shows the question when ensureGitChoice asks. */
export function GitChoiceHost() {
  const [pending, setPending] = useState<Pending | null>(null);
  useEffect(() => { ask = setPending; return () => { ask = null; }; }, []);
  if (!pending) return null;
  const finish = (go: boolean) => { pending.resolve(go); setPending(null); };
  return <GitChoiceDialog ws={pending.ws} onDone={finish} />;
}

function GitChoiceDialog({ ws, onDone }: { ws: Workspace; onDone: (go: boolean) => void }) {
  const [rows, setRows] = useState<GitCredential[] | null>(null), [choice, setChoice] = useState<string>("");
  const [draft, setDraft] = useState<GitCredentialInput>(blank()), [busy, setBusy] = useState(false), [error, setError] = useState("");
  useEffect(() => {
    api.gitCredentials.list().then((list) => { setRows(list); setChoice(list.length === 1 ? list[0].id : list.length ? "" : "new"); })
      .catch((e: Error) => setError(e.message));
  }, []);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      const id = choice === "new" ? (await api.gitCredentials.create(draft)).id : choice === "none" ? null : choice;
      await api.setWorkspaceGit(ws.id, id);
      onDone(true);
    } catch (err) { setError((err as Error).message); setBusy(false); }
  };
  return <div className="modal-backdrop">
    <form className="modal wide" role="dialog" aria-modal="true" aria-labelledby="git-choice-title" onSubmit={(e) => void submit(e)}
          onKeyDown={(e) => { if (e.key === "Escape") onDone(false); }}>
      <h2 id="git-choice-title">Git for {ws.name}</h2>
      <p className="muted">Which of your Git credentials should this workspace use? It is set up when the workspace starts: no tokens or settings to type in the editor. You can change it later on the workspace's page.</p>
      {rows === null ? <p className="muted">Loading…</p> : <fieldset className="plain" aria-label="Git credential">
        {rows.map((r) => <label key={r.id} className="row"><input type="radio" name="git-choice" checked={choice === r.id} onChange={() => setChoice(r.id)} />
          <span><strong>{r.label}</strong> <span className="muted">{r.username} on {r.host}{r.commit_email && ` · commits as ${r.commit_email}`}</span></span></label>)}
        <label className="row"><input type="radio" name="git-choice" checked={choice === "new"} onChange={() => setChoice("new")} />Add a new credential</label>
        <label className="row"><input type="radio" name="git-choice" checked={choice === "none"} onChange={() => setChoice("none")} />No Git in this workspace</label>
      </fieldset>}
      {choice === "new" && <CredentialFields value={draft} onChange={setDraft} editing={false} idPrefix="gcw" />}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="form-actions"><button className="btn" autoFocus disabled={busy || !choice || (choice === "new" && !valid(draft, false))}>{busy ? "Saving…" : "Use and start"}</button>
        <button type="button" className="btn secondary" onClick={() => onDone(false)}>Cancel</button></div>
    </form>
  </div>;
}

/** The workspace page's Git line: which credential it uses, changeable at any time. */
export function WorkspaceGit({ ws, onChanged }: { ws: Workspace; onChanged: () => Promise<void> | void }) {
  const [rows, setRows] = useState<GitCredential[]>([]), [error, setError] = useState("");
  useEffect(() => { api.gitCredentials.list().then(setRows).catch((e: Error) => setError(e.message)); }, []);
  if (!usesGit(ws)) return null;
  const change = async (value: string) => {
    setError("");
    try { await api.setWorkspaceGit(ws.id, value || null); await onChanged(); } catch (e) { setError((e as Error).message); }
  };
  return <div className="field" style={{ maxWidth: 520 }}>
    <label htmlFor={`ws-git-${ws.id}`}>Git credential</label>
    <select id={`ws-git-${ws.id}`} value={ws.git_asked ? ws.git_credential_id ?? "" : "ask"} onChange={(e) => void change(e.target.value)}>
      {!ws.git_asked && <option value="ask" disabled>Not chosen yet (asked at the next start)</option>}
      <option value="">No Git</option>
      {rows.map((r) => <option key={r.id} value={r.id}>{r.label} · {r.username} on {r.host}</option>)}
    </select>
    <span className="hint">The token switches right away; the commit name and email at the next start. Manage credentials under Account → Git credentials.</span>
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}
