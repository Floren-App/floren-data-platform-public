import { useEffect, useState } from "react";
import { api, type BackupConfiguration, type DataSource, type BackupRecord } from "../api";
import type { Hub } from "../hub";
import { can } from "../permissions";
import { PageHeader } from "../components/Page";

export function BackupsView({ hub }: { hub: Hub }) {
  const [config, setConfig] = useState<BackupConfiguration | null>(null);
  const [history, setHistory] = useState<BackupRecord[]>([]);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const configurable = hub.me?.acting_admin === true;
  const historyAllowed = can(hub.me, "backups.history.read", "set-backups");
  const configurationAllowed = can(hub.me, "backups.configuration.read", "set-backups");
  const refresh = async () => {
    try {
      if (configurationAllowed) setConfig(await api.backups.configuration());
      if (historyAllowed) setHistory(await api.backups.history());
    } catch (e) { setError((e as Error).message); }
  };
  useEffect(() => { void refresh(); }, [configurationAllowed, historyAllowed]);
  useEffect(() => { if (configurable) api.admin.sources().then(setSources).catch(e => setError((e as Error).message)); }, [configurable]);
  const save = async (e: React.FormEvent) => {
    e.preventDefault(); if (!config) return; setBusy(true); setError(""); setMessage("");
    try { setConfig(await api.backups.configure(config)); setMessage("Backup configuration saved."); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return <>
    <PageHeader title="Backup and recovery" subtitle="Encrypted recovery archives for platform metadata and signing keys. Customer data systems, identity provider, secret store and persistent volumes need their own backups."
      actions={<div className="row"><button className="btn secondary" onClick={() => void refresh()}>Refresh</button>
        {can(hub.me, "backups.create", "set-backups") && <button className="btn" disabled={busy} onClick={async () => {
          setBusy(true); setError(""); try { await api.backups.create(); setMessage("Backup queued. Refresh to check verification status."); await refresh(); }
          catch (e) { setError((e as Error).message); } finally { setBusy(false); }
        }}>Create backup</button>}</div>} />
    {error && <p role="alert" className="error">{error}</p>}
    {message && <p role="status">{message}</p>}
    {config && <form className="card" onSubmit={save}><h2>Backup destination</h2>
      <fieldset disabled={!configurable || busy}>
        <label className="row"><input type="checkbox" checked={config.enabled} onChange={e => setConfig({ ...config, enabled:e.target.checked })} />Enable scheduled backups</label>
        <div className="arg-grid">
          <div className="field"><label htmlFor="backup-source">Existing S3 integration</label><select id="backup-source" value={config.source_id} onChange={e => setConfig({ ...config, source_id:e.target.value, bucket:"", endpoint_url:"" })}>
            <option value="">Dedicated destination using installation workload identity</option>
            {sources.filter(s => s.kind === "s3" && !s.config.managed_rustfs).map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select></div>
          {!config.source_id && <>
            <div className="field"><label htmlFor="backup-bucket">S3 bucket</label><input id="backup-bucket" value={config.bucket} onChange={e => setConfig({ ...config, bucket:e.target.value })} /></div>
            <div className="field"><label htmlFor="backup-endpoint">S3 endpoint (optional)</label><input id="backup-endpoint" type="url" placeholder="https://s3.example.com" value={config.endpoint_url} onChange={e => setConfig({ ...config, endpoint_url:e.target.value })} /></div>
            <div className="field"><label htmlFor="backup-region">Region</label><input id="backup-region" value={config.region} onChange={e => setConfig({ ...config, region:e.target.value })} /></div>
          </>}
          <div className="field"><label htmlFor="backup-prefix">Backup prefix</label><input id="backup-prefix" required value={config.prefix} onChange={e => setConfig({ ...config, prefix:e.target.value })} /></div>
          <div className="field"><label htmlFor="backup-interval">Interval (hours)</label><input id="backup-interval" type="number" min={1} max={720} value={config.interval_hours} onChange={e => setConfig({ ...config, interval_hours:Number(e.target.value) })} /></div>
          <div className="field"><label htmlFor="backup-sse">Additional S3 encryption</label><select id="backup-sse" value={config.server_side_encryption} onChange={e => setConfig({ ...config, server_side_encryption:e.target.value, kms_key_id:"" })}>
            <option value="">Bucket default</option><option value="AES256">SSE-S3</option><option value="aws:kms">SSE-KMS</option></select></div>
          {config.server_side_encryption === "aws:kms" && <div className="field"><label htmlFor="backup-kms">KMS key (optional)</label><input id="backup-kms" value={config.kms_key_id} onChange={e => setConfig({ ...config, kms_key_id:e.target.value })} /></div>}
          <div className="field"><label htmlFor="backup-lock">Object Lock retention (days)</label><input id="backup-lock" type="number" min={0} max={36500} value={config.object_lock_days} onChange={e => setConfig({ ...config, object_lock_days:Number(e.target.value) })} /></div>
        </div>
        <div className="field"><label htmlFor="backup-public-key">Recovery public key (RSA, 3072 bits or more)</label><textarea id="backup-public-key" rows={7} value={config.recovery_public_key} onChange={e => setConfig({ ...config, recovery_public_key:e.target.value })} /></div>
        <p className="hint">Keep the private recovery key offline. Object Lock requires a bucket already configured for it; compliance retention cannot be shortened. Configure archive expiry with the bucket's lifecycle policy.</p>
        {configurable && <button className="btn" disabled={busy}>Save configuration</button>}
      </fieldset>
    </form>}
    {historyAllowed && <><h2>Backup history</h2><table aria-label="Platform backups"><thead><tr><th>Created</th><th>Status</th><th>S3 object</th><th>Size</th><th>SHA-256</th></tr></thead><tbody>
      {history.map(row => <tr key={row.id}><td>{new Date(row.created_at).toLocaleString()}</td><td>{row.status}{row.error && <p className="error-text">{row.error}</p>}</td>
        <td>{row.object_key ? `s3://${row.bucket}/${row.object_key}` : "—"}</td><td>{row.size_bytes ? `${(row.size_bytes / 1048576).toFixed(1)} MB` : "—"}</td><td><code>{row.sha256 || "—"}</code></td></tr>)}
      {!history.length && <tr><td colSpan={5}>No platform backups yet.</td></tr>}
    </tbody></table></>}
    <p className="scope-note">Restore is an offline operator procedure into an empty database. A restored installation remains quarantined until identities, permissions, expired approvals and dependencies are reconciled.</p>
  </>;
}
