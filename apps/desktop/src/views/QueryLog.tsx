import { useCallback, useEffect, useState } from "react";
import { api, QueryLogRow, QueryLogSettings } from "../api";
import type { Hub } from "../hub";
import { PageHeader } from "../components/Page";
import { IconRefresh } from "../components/Icons";
import { ProductIcon } from "../components/ProductIcon";

const ms = (n: number) => n < 1000 ? `${n} ms` : n < 60000 ? `${(n / 1000).toFixed(1)} s` : `${(n / 60000).toFixed(1)} min`;
const bytes = (n: number) => n < 1024 ? `${n} B` : n < 1024 ** 2 ? `${(n / 1024).toFixed(0)} KB` : n < 1024 ** 3 ? `${(n / 1024 ** 2).toFixed(1)} MB` : `${(n / 1024 ** 3).toFixed(1)} GB`;

/** Every query on the layers' Trino: who or what ran it, on which tables, how long and with what result. */
export function QueryLogView({ hub, initialActor = "" }: { hub: Hub; initialActor?: string }) {
  const [rows, setRows] = useState<QueryLogRow[]>([]), [error, setError] = useState("");
  const [filters, setFilters] = useState({ actor: initialActor, layer_id: "", state: "", table: "" });
  const [settings, setSettings] = useState<QueryLogSettings | null>(null), [saved, setSaved] = useState("");
  const reload = useCallback(() => api.queryLog(filters).then((r) => { setRows(r); setError(""); }).catch((e: Error) => setError(e.message)), [filters]);
  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => { if (hub.me?.is_admin) api.queryLogSettings().then(setSettings).catch(() => undefined); }, [hub.me?.is_admin]);
  const layerName = (id: string) => hub.layers.find((l) => l.id === id)?.name ?? id;
  const context = (r: QueryLogRow) => r.workload_id ? `workload ${r.workload_id}` : r.run_id ? `run ${r.run_id}` : r.workspace_id ? `workspace ${r.workspace_id}` : r.source || "";
  const save = async () => {
    if (!settings) return; setSaved("");
    try { setSettings(await api.saveQueryLogSettings(settings)); setSaved("Saved."); } catch (e) { setError((e as Error).message); }
  };
  return <>
    <PageHeader icon={<ProductIcon kind="table" size={22} />} title="Query log"
      subtitle="Every query that ran on a layer's Trino: who or what ran it (a person, a service account, a run, workload or workspace), the tables it read or wrote, how long it took and how it ended. Administrators see all queries; everyone else their own."
      actions={<button className="btn secondary" onClick={() => void reload()}><IconRefresh size={14} />Refresh</button>} />
    {error && <p className="error" role="alert">{error}</p>}
    <div className="arg-grid" role="search" aria-label="Filter queries">
      <div className="field"><label htmlFor="ql-actor">Who</label><input id="ql-actor" value={filters.actor} onChange={(e) => setFilters({ ...filters, actor: e.target.value })} placeholder="person or service" /></div>
      <div className="field"><label htmlFor="ql-table">Table</label><input id="ql-table" value={filters.table} onChange={(e) => setFilters({ ...filters, table: e.target.value })} placeholder="orders" /></div>
      <div className="field"><label htmlFor="ql-layer">Layer</label><select id="ql-layer" value={filters.layer_id} onChange={(e) => setFilters({ ...filters, layer_id: e.target.value })}>
        <option value="">All layers</option>{hub.layers.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select></div>
      <div className="field"><label htmlFor="ql-state">Result</label><select id="ql-state" value={filters.state} onChange={(e) => setFilters({ ...filters, state: e.target.value })}>
        <option value="">All</option><option value="FINISHED">Finished</option><option value="FAILED">Failed</option></select></div>
    </div>
    {!rows.length ? <p className="muted">No queries yet{Object.values(filters).some(Boolean) ? " matching these filters" : ""}.</p>
    : <table aria-label="Queries"><thead><tr><th>When</th><th>Who</th><th>Tables</th><th>Result</th><th>Time</th><th>Rows</th></tr></thead><tbody>
      {rows.map((r) => <tr key={r.id}>
        <td>{new Date(r.created_at).toLocaleString()}<div className="muted">{layerName(r.layer_id)}</div></td>
        <td><strong>{r.actor || r.trino_user}</strong><div className="muted">{context(r)}</div></td>
        <td className="mono">{r.tables.join(", ")}{r.output_table && <div>→ {r.output_table}</div>}
          {r.sql_text && <details><summary>SQL</summary><pre className="code">{r.sql_text}</pre></details>}</td>
        <td><span className={`badge status-${r.state.toLowerCase()}`}>{r.state === "FINISHED" ? "Finished" : r.state === "FAILED" ? "Failed" : r.state}</span>
          {r.error && <div className="muted" title={r.error}>{r.error.slice(0, 120)}</div>}</td>
        <td>{ms(r.wall_ms)}<div className="muted">CPU {ms(r.cpu_ms)}</div></td>
        <td>{r.rows.toLocaleString()}<div className="muted">{bytes(r.bytes)}</div></td>
      </tr>)}
    </tbody></table>}
    {settings && <form className="card" aria-label="Query log settings" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <h3>Settings</h3>
      <div className="arg-grid">
        <div className="field"><label htmlFor="ql-days">Keep queries for (days)</label>
          <input id="ql-days" type="number" min={1} max={3650} value={settings.retention_days} onChange={(e) => setSettings({ ...settings, retention_days: Number(e.target.value) })} /></div>
      </div>
      <label className="row"><input type="checkbox" checked={settings.record_sql} onChange={(e) => setSettings({ ...settings, record_sql: e.target.checked })} />
        Keep each query's SQL text <span className="muted">(off by default: queries can contain literal values such as personal data)</span></label>
      <p className="muted">Also configurable with OpenTofu (fdp_query_log_settings).</p>
      <div className="form-actions"><button className="btn">Save settings</button>{saved && <span className="muted" role="status">{saved}</span>}</div>
    </form>}
  </>;
}
