import { useState } from "react";
import { api, AccessResource } from "../api";
import type { Hub } from "../hub";
import { useObjectCache } from "../cache";

export function MetadataSql({ schema, hub }: { schema: AccessResource; hub: Hub }) {
  const cache = useObjectCache();
  const catalog = String(schema.config.catalog);
  const prefix = `"${catalog}"."${schema.name}"`;
  const [sql, setSql] = useState(`SELECT id, name, description FROM ${prefix}.app_groups ORDER BY name`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.metadataQuery>> | null>(null);
  const run = async () => {
    setBusy(true); setError(""); setResult(null);
    try {
      const data = await api.metadataQuery(sql);
      setResult(data);
      cache.invalidate("data:preview:");
      await hub.refresh();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <section aria-label="Platform metadata SQL">
    <h2>Platform metadata SQL</h2>
    <p className="muted">Query live metadata through Trino. Changes to groups, identity bindings, permissions, governance and SLAs use the same validation as the app. Audit history and runtime state are read-only.</p>
    <label htmlFor="metadata-sql">SQL statement</label>
    <textarea id="metadata-sql" className="mono" rows={9} value={sql} onChange={(e) => setSql(e.target.value)} spellCheck={false} style={{ width: "100%" }} />
    <div className="form-actions"><button className="btn" disabled={busy || !sql.trim()} onClick={run}>{busy ? "Running…" : "Run SQL"}</button></div>
    {error && <p className="error" role="alert">{error}</p>}
    {result && <><p role="status">{result.rowcount} rows{result.truncated ? " · showing the first 1,000" : ""}</p>
      <div className="preview-table"><table><thead><tr>{result.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead><tbody>
        {result.rows.map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j}>{v === null ? "NULL" : typeof v === "object" ? JSON.stringify(v) : String(v)}</td>)}</tr>)}
      </tbody></table></div></>}
    <details className="card"><summary>SQL examples</summary>
      <pre className="code">{`-- Find resources and identities\nSELECT id, name, kind FROM ${prefix}.rbac_resources;\nSELECT id, name, kind, provider FROM ${prefix}.directory_principals;\n\n-- Grant a built-in role (replace IDs and group name)\nGRANT Reader ON RESOURCE 'resource-id' TO GROUP 'Finance Readers';\nREVOKE Reader ON RESOURCE 'resource-id' FROM GROUP 'Finance Readers';\n\n-- Direct managed identity grant\nINSERT INTO ${prefix}.service_role_assignments\n  (resource_id, principal_id, role)\nVALUES ('schema-resource-id', 'service-principal-id', 'Contributor');\n\n-- Update a group\nUPDATE ${prefix}.app_groups\nSET description = 'Finance reporting readers' WHERE id = 'group-id';`}</pre>
    </details>
  </section>;
}
