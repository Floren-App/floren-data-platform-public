import { useCallback, useEffect, useRef, useState } from "react";
import { ago, api, LogEntry, LogQuery, shortSha } from "../api";
import type { TabRef } from "../views/Tabs";

const LEVELS: [string, string][] = [["debug", "All levels"], ["info", "Info and up"], ["warning", "Warnings and errors"], ["error", "Errors only"]];

/** The central log for a workload, a run, or a Workloads folder (the whole tree for "workloads"): newest first,
 *  live while open. Platform lines (runs, deploys, Airflow DAG runs) and program lines (fdp.log) together. */
export function LogView({ scope, layerId, openTab, showWorkload = true, label = "Log" }: {
  scope: Pick<LogQuery, "workload_id" | "folder_id" | "run_id">; layerId?: string; openTab: (tab: TabRef) => void; showWorkload?: boolean; label?: string;
}) {
  const [entries, setEntries] = useState<LogEntry[] | null>(null), [older, setOlder] = useState<number | null>(null);
  const [level, setLevel] = useState("debug"), [search, setSearch] = useState(""), [query, setQuery] = useState("");
  const [live, setLive] = useState(true), [error, setError] = useState(""), [open, setOpen] = useState<number | null>(null);
  const key = JSON.stringify([scope, layerId, level, query]);
  const current = useRef(key); current.current = key;
  const load = useCallback(async () => {
    const asked = key;
    try {
      const page = await api.logs({ ...scope, layer_id: layerId, level, q: query, limit: 100 });
      if (current.current !== asked) return;
      // Keep older pages already loaded: merge the newest page in front.
      setEntries((before) => { const seen = new Set(page.entries.map((e) => e.id));
        return before && before.length > page.entries.length ? [...page.entries, ...before.filter((e) => !seen.has(e.id) && e.id < (page.entries[page.entries.length - 1]?.id ?? 0))] : page.entries; });
      setOlder((o) => o ?? page.next_before); setError("");
    } catch (e) { if (current.current === asked) setError((e as Error).message); }
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setEntries(null); setOlder(null); void load(); }, [load]);
  useEffect(() => { if (!live) return; const t = setInterval(() => { if (document.visibilityState !== "hidden") void load(); }, 5000); return () => clearInterval(t); }, [live, load]);
  const more = async () => {
    if (!older) return;
    try { const page = await api.logs({ ...scope, layer_id: layerId, level, q: query, before: older, limit: 100 });
      setEntries((before) => [...(before ?? []), ...page.entries]); setOlder(page.next_before); }
    catch (e) { setError((e as Error).message); }
  };
  const run = (e: LogEntry) => e.run_id && openTab({ kind: "run", ref: e.run_id, title: e.workload || "Run", runScope: e.workload_id ? "deployed" : "workspace" });
  return <section className="log-view" aria-label={label}>
    <div className="object-toolbar">
      <input type="search" aria-label="Search the log" placeholder="Search messages…" value={search} onChange={(e) => setSearch(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") setQuery(search.trim()); }} onBlur={() => setQuery(search.trim())} />
      <select aria-label="Log level" value={level} onChange={(e) => setLevel(e.target.value)}>{LEVELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
      <label className="row muted"><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />Live</label>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {!entries ? <p className="muted" role="status">Loading the log…</p>
      : !entries.length ? <p className="muted">{query || level !== "debug" ? "No log lines match." : <>Nothing logged yet. Runs, deploys and Airflow DAG runs appear here; programs add lines with <code>fdp.log.info(…)</code> (Python) or <code>fdp::log_info(…)</code> (R).</>}</p>
      : <div className="table-scroll"><table className="log-table" aria-label="Log lines"><thead><tr><th>Time</th><th>Level</th>{showWorkload && <th>Workload</th>}<th>Message</th><th>Version</th><th /></tr></thead><tbody>
        {entries.map((e) => <tr key={e.id} className={`log-${e.level}`}>
          <td className="muted" title={new Date(e.at).toLocaleString()}>{ago(e.at)}</td>
          <td><span className={`log-level ${e.level}`}>{e.level}</span></td>
          {showWorkload && <td>{e.workload_id ? <button className="text-button" onClick={() => openTab({ kind: "workload", ref: e.workload_id!, title: e.workload || "Workload", section: "logs" })}>{e.workload || e.workload_id}</button> : <span className="muted">workspace</span>}</td>}
          <td className="log-message"><span className="muted log-source">{e.source === "program" ? "" : e.source === "airflow" ? "Airflow · " : "Platform · "}</span>{e.message}
            {Object.keys(e.fields).length > 0 && <> <button className="text-button" aria-expanded={open === e.id} onClick={() => setOpen(open === e.id ? null : e.id)}>{open === e.id ? "hide" : "details"}</button></>}
            {open === e.id && <pre className="code log-fields">{JSON.stringify(e.fields, null, 2)}</pre>}</td>
          <td className="mono" title={e.commit_sha ?? undefined}>{e.commit_sha ? shortSha(e.commit_sha) : <span className="muted">—</span>}</td>
          <td>{e.run_id && <button className="text-button" onClick={() => run(e)}>Run</button>}</td>
        </tr>)}
      </tbody></table></div>}
    {entries && older && <div className="row pagination"><button className="btn sm secondary" onClick={() => void more()}>Load older lines</button></div>}
  </section>;
}
