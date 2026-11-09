import { useEffect, useState } from "react";
import { api, InstallationReadiness } from "../api";
import { PageHeader } from "../components/Page";
import { guideUrl } from "../documentation";

const STATE: Record<string, string> = {
  configured: "Configured", attention: "Needs attention", not_configured: "Not configured",
  single_instance: "Single instance", customer_operated: "Operated by you", unknown: "Unknown",
};
const TONE: Record<string, string> = { configured: "ready", attention: "unavailable", not_configured: "unknown",
  single_instance: "unavailable", customer_operated: "unknown", unknown: "unknown" };

/** What is configured, protected, operated by someone else, or unknown: evidence, not settings. Nothing here
 * changes the installation; optional features that are off are said to be off. */
export function InstallationReadinessView() {
  const [report, setReport] = useState<InstallationReadiness | null>(null), [error, setError] = useState("");
  const load = () => api.admin.installationReadiness().then((r) => { setReport(r); setError(""); }).catch((e: Error) => setError(e.message));
  useEffect(() => { void load(); }, []);
  const areas = report ? [...new Set(report.items.map((i) => i.area))] : [];
  return <>
    <PageHeader title="Installation readiness" subtitle="What this installation has configured and evidenced, what someone else operates, and what is unknown. Read-only."
      actions={<button className="btn secondary" onClick={() => void load()}>Check again</button>} />
    {error && <p className="error" role="alert">{error}</p>}
    {!report && !error && <p className="muted loading" role="status">Checking…</p>}
    {report && <>
      <p className="muted small">Release {report.release} · checked {new Date(report.generated_at).toLocaleString()} ·{" "}
        {Object.entries(report.summary).filter(([, n]) => n).map(([state, n]) => `${n} ${STATE[state].toLowerCase()}`).join(", ")}</p>
      {areas.map((area) => <section key={area} className="card" aria-label={area}><h2>{area}</h2>
        <ul className="readiness-checks">{report.items.filter((i) => i.area === area).map((i) => <li key={i.item}>
          <span className={`readiness-state ${TONE[i.state]}`}>{STATE[i.state] ?? i.state}</span>
          <span><strong>{i.item}</strong>{i.detail && <span className="muted"> · {i.detail}</span>}</span></li>)}</ul></section>)}
      <p className="hint">Single-instance bundled services are not highly available; bring your own for that. Backups protect only what
        their plans cover, and a completed backup is not a tested restore: see <a href={guideUrl("platform-recovery/")} target="_blank" rel="noreferrer">recovery</a>.</p>
    </>}
  </>;
}
