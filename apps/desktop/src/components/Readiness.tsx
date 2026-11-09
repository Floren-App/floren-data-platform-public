import { useCallback, useEffect, useState } from "react";
import { api, ReadinessCheck, WorkspaceReadiness } from "../api";

const STATE_LABEL: Record<ReadinessCheck["state"], string> = { ready: "Ready", missing: "Missing", unavailable: "Not available", unknown: "Unknown" };
const KIND_LABEL = { person: "you", editor: "this workspace's editor", job: "a run", workload: "a workload", service: "a service identity" };

/** What will work in this workspace and as whom: who acts, which group's access applies, and each capability. It
 * explains before you act; the actions still check for themselves. */
export function ReadinessPanel({ workspaceId, status }: { workspaceId: string; status: string }) {
  const [readiness, setReadiness] = useState<WorkspaceReadiness | null>(null), [error, setError] = useState(""), [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    let live = true;
    setLoading(true);
    api.workspaceReadiness(workspaceId)
      .then((r) => { if (live) { setReadiness(r); setError(""); } })
      .catch((e) => { if (live) setError((e as Error).message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [workspaceId]);
  useEffect(load, [load, status]);
  const context = readiness?.context;
  const differs = readiness && readiness.workspace_group_id && context?.group_id && readiness.workspace_group_id !== context.group_id;
  const missing = readiness?.checks.filter((c) => c.state !== "ready").length ?? 0;
  return <section className="card readiness" aria-label="Readiness" aria-busy={loading}>
    <div className="section-heading"><h2>Readiness{readiness && <span className="muted"> · {missing ? `${missing} to look at` : "all ready"}</span>}</h2>
      <button className="btn sm secondary" disabled={loading} onClick={() => load()}>{loading ? "Checking…" : "Check again"}</button></div>
    {loading && !readiness && <p className="loading">Checking what works here…</p>}
    {error && <p className="error" role="alert">Readiness could not be checked: {error}</p>}
    {context && <p className="readiness-context">Acting as <strong>{context.group_name ?? "no group"}</strong>
      {context.layer_name && <> in <strong>{context.layer_name}</strong></>} <span className="muted">({KIND_LABEL[context.kind]})</span>.
      {differs && <span className="muted"> The editor and its runs act as <strong>{readiness!.workspace_group_name}</strong>, the group the workspace was created for, so their access can differ from yours here.</span>}</p>}
    {readiness && <ul className="readiness-checks">
      {readiness.checks.map((c) => <li key={c.key} className={`readiness-${c.state}`}>
        <span className={`readiness-state ${c.state}`}>{STATE_LABEL[c.state]}</span>
        <span><strong>{c.label}</strong>{c.detail && <span className="muted"> · {c.detail}</span>}
          {c.permission && c.state === "missing" && <> <code title="The permission an administrator can grant">{c.permission}</code></>}</span>
      </li>)}
    </ul>}
    {readiness && <p className="small muted">Checked {new Date(readiness.checked_at).toLocaleTimeString()}. This explains what to expect; each action still checks your access when you take it.</p>}
  </section>;
}
