import { useEffect, useState } from "react";
import { api, type BackupPlan, type BackupPlanInput, type BackupDestination, type BackupDestinationInput, type BackupSource, type BackupRecord, type DataSource, type BackupConfiguration } from "../api";
import type { Hub } from "../hub";
import { can } from "../permissions";
import { PageHeader } from "../components/Page";

const newPlan = (): BackupPlanInput => ({ name:"", enabled:false, source_kind:"control_plane", source_id:"", destination_id:"", prefix:"daily", schedule:"0 2 * * *", timezone:Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC", schemas:[], source_prefix:"", retention_days:30, max_bytes:10737418240, max_objects:100000 });
const newDestination = (): BackupDestinationInput => ({ name:"", bucket:"", prefix:"backups", endpoint_url:"", region:"us-east-1", source_id:"", recovery_key_ref:"", server_side_encryption:"", kms_key_id:"", object_lock_days:0 });

export function BackupsView({ hub }: { hub: Hub }) {
  const [plans, setPlans] = useState<BackupPlan[]>([]), [destinations, setDestinations] = useState<BackupDestination[]>([]);
  const [integrations, setIntegrations] = useState<DataSource[]>([]);
  const [legacy, setLegacy] = useState<BackupConfiguration | null>(null), [legacyHistory, setLegacyHistory] = useState<BackupRecord[]>([]);
  const [sources, setSources] = useState<BackupSource[]>([]), [history, setHistory] = useState<BackupRecord[]>([]);
  const [historyPlan, setHistoryPlan] = useState<BackupPlan | null>(null);
  const [plan, setPlan] = useState<BackupPlanInput | null>(null), [planId, setPlanId] = useState<string>();
  const [destination, setDestination] = useState<BackupDestinationInput | null>(null), [destinationId, setDestinationId] = useState<string>();
  const [error, setError] = useState(""), [message, setMessage] = useState(""), [busy, setBusy] = useState(false);
  const allowed = (action: string, id?: string) => {
    const p = plans.find(p => p.id === id);
    if(p?.capabilities) return p.capabilities.includes(action);
    const layer = p?.layer_id;
    return can(hub.me,action,id || "set-backups",layer) || (!!id && can(hub.me,action,"set-backups",layer));
  };
  const refresh = async () => {
    const [p,d,s] = await Promise.all([api.backups.plans(),api.backups.destinations(),api.backups.sources()]);
    setPlans(p);setDestinations(d);setSources(s);
    if(can(hub.me,"backups.configuration.read","set-backups")) setLegacy(await api.backups.configuration());
    if(can(hub.me,"backups.history.read","set-backups")) setLegacyHistory((await api.backups.history()).filter(r=>!r.plan_id));
    if (historyPlan) setHistory(await api.backups.planHistory(historyPlan.id));
  };
  useEffect(() => { refresh().catch(e => setError((e as Error).message)); }, []);
  useEffect(() => { if (hub.me?.acting_admin) api.admin.sources().then(setIntegrations).catch(e => setError((e as Error).message)); }, [hub.me?.acting_admin]);
  const act = async (work: () => Promise<unknown>, done: string) => {
    setBusy(true);setError("");setMessage("");
    try { await work();setMessage(done);await refresh(); } catch(e) {setError((e as Error).message);} finally {setBusy(false);}
  };
  const editPlan = (p?: BackupPlan) => {
    if (p) { const {id,next_run_at,layer_id,capabilities,...body}=p;setPlan(body);setPlanId(id); }
    else {const draft=newPlan();if(!sources.some(s=>s.kind==="control_plane")){draft.source_kind="storage";draft.source_id=sources.find(s=>s.supported)?.id || "";}setPlan(draft);setPlanId(undefined);}
    setError("");
  };
  const editDestination = (d?: BackupDestination) => {
    if (d) { const {id,...body}=d;setDestination(body);setDestinationId(id); }
    else {setDestination(newDestination());setDestinationId(undefined);}
    setError("");
  };
  const selectedSource = plan && sources.find(s => s.id === plan.source_id && s.kind !== "control_plane");
  return <>
    <PageHeader title="Backup and recovery" subtitle="Independent plans for the control plane and supported storage integrations. Each plan has its own destination, path, schedule and history."
      actions={<div className="row"><button className="btn secondary" onClick={() => void act(refresh,"Refreshed.")}>Refresh</button>
        {(allowed("backups.plans.create") || hub.me?.permission_grants?.some(g=>g.actions.includes("backups.plans.create"))) && <button className="btn" onClick={() => editPlan()}>New backup plan</button>}
        {hub.me?.acting_admin && <button className="btn secondary" onClick={() => editDestination()}>New destination</button>}</div>} />
    {error && <p role="alert" className="error">{error}</p>}{message && <p role="status">{message}</p>}
    {(legacy?.enabled || legacyHistory.length > 0) && <section className="card"><h2>Legacy control-plane backup</h2><p>{legacy?.enabled ? `Enabled: every ${legacy.interval_hours} hours` : "Schedule paused"}. Existing archive history is retained; new independent plans do not enable or replace this schedule.</p>{legacy?.enabled && hub.me?.acting_admin && <button className="btn secondary" disabled={busy} onClick={()=>void act(()=>api.backups.disableConfiguration(),"Legacy schedule paused; archives retained.")}>Pause legacy schedule</button>}<p>{legacyHistory.length} existing control-plane backup records.</p></section>}
    <h2>Backup plans</h2>
    <div className="table-scroll"><table aria-label="Backup plans"><thead><tr><th>Plan</th><th>Source</th><th>Destination and path</th><th>Schedule</th><th>Status</th><th /></tr></thead><tbody>
      {plans.map(p => {const d=destinations.find(d => d.id===p.destination_id);return <tr key={p.id}>
        <td>{p.name}</td><td>{p.source_kind === "control_plane" ? "Control plane" : sources.find(s => s.id===p.source_id)?.name || p.source_id}</td>
        <td>{d ? `s3://${d.bucket || "integration"}/${d.prefix}/${p.prefix}/` : p.destination_id}<div className="muted small">{p.retention_days ? `${p.retention_days} days retention` : "Keep indefinitely"}</div></td>
        <td><code>{p.schedule}</code> · {p.timezone}{p.enabled && p.next_run_at && <div className="muted small">Next: {new Date(p.next_run_at).toLocaleString(undefined,{timeZone:p.timezone})}</div>}</td>
        <td>{p.enabled ? "Scheduled" : "Paused"}</td><td className="table-actions">
          {allowed("backups.plans.run",p.id) && <button className="btn sm" disabled={busy} onClick={() => void act(() => api.backups.runPlan(p.id),"Backup queued.")}>Run now</button>}
          {allowed("backups.plans.update",p.id) && <button className="btn sm secondary" onClick={() => editPlan(p)}>Edit</button>}
          {allowed("backups.history.read",p.id) && <button className="btn sm secondary" onClick={() => void act(async () => {setHistoryPlan(p);setHistory(await api.backups.planHistory(p.id));},"History loaded.")}>History</button>}
          {allowed("backups.plans.delete",p.id) && <button className="btn sm secondary" disabled={busy} onClick={() => void act(() => api.backups.deletePlan(p.id),"Plan removed; existing archives retained.")}>Remove plan</button>}
        </td></tr>;})}
      {!plans.length && <tr><td colSpan={6} className="muted">No backup plans. Plans start paused until you enable their schedule.</td></tr>}
    </tbody></table></div>
    {plan && <section className="card" aria-label="Backup plan editor"><h2>{planId ? "Edit backup plan" : "New backup plan"}</h2>
      <form onSubmit={e => {e.preventDefault();void act(async () => {await api.backups.savePlan(plan,planId);setPlan(null);},"Backup plan saved.");}}>
        <fieldset disabled={busy}><div className="arg-grid">
          <div className="field"><label htmlFor="backup-plan-name">Plan name</label><input id="backup-plan-name" required value={plan.name} onChange={e => setPlan({...plan,name:e.target.value})} /></div>
          <div className="field"><label htmlFor="backup-plan-source">What to back up</label><select id="backup-plan-source" value={plan.source_kind === "control_plane" ? "control_plane" : plan.source_id} onChange={e => setPlan({...plan,source_kind:e.target.value === "control_plane" ? "control_plane" : "storage",source_id:e.target.value === "control_plane" ? "" : e.target.value,schemas:[],source_prefix:""})}>
            <option value="" disabled>Choose a source</option>{sources.map(s => <option key={s.id || s.kind} value={s.id || s.kind} disabled={!s.supported}>{s.name}{!s.supported ? " — adapter unavailable" : ""}</option>)}</select>
            {selectedSource && <p className="hint">{selectedSource.detail}</p>}</div>
          <div className="field"><label htmlFor="backup-plan-destination">Destination</label><select id="backup-plan-destination" required value={plan.destination_id} onChange={e => setPlan({...plan,destination_id:e.target.value})}>
            <option value="">Choose a destination</option>{destinations.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></div>
          <div className="field"><label htmlFor="backup-plan-path">Path below destination</label><input id="backup-plan-path" required value={plan.prefix} onChange={e => setPlan({...plan,prefix:e.target.value})} /></div>
          {(selectedSource?.kind === "postgres" || selectedSource?.kind === "iceberg") && <div className="field"><label htmlFor="backup-schemas">Schemas / Iceberg namespaces (comma separated; empty means all)</label><input id="backup-schemas" value={plan.schemas.join(", ")} onChange={e => setPlan({...plan,schemas:e.target.value.split(",").map(s => s.trim()).filter(Boolean)})} /></div>}
          {selectedSource?.kind === "s3" && <div className="field"><label htmlFor="backup-source-prefix">Source prefix (relative to integration)</label><input id="backup-source-prefix" value={plan.source_prefix} onChange={e => setPlan({...plan,source_prefix:e.target.value})} /></div>}
          <div className="field"><label htmlFor="backup-time">Daily backup time</label><input id="backup-time" type="time" value={/^\d+ \d+ \* \* \*$/.test(plan.schedule) ? `${plan.schedule.split(" ")[1].padStart(2,"0")}:${plan.schedule.split(" ")[0].padStart(2,"0")}` : ""} onChange={e => {if(e.target.value){const [h,m]=e.target.value.split(":");setPlan({...plan,schedule:`${Number(m)} ${Number(h)} * * *`});}}} /></div>
          <div className="field"><label htmlFor="backup-cron">Schedule (five-field cron)</label><input id="backup-cron" required value={plan.schedule} onChange={e => setPlan({...plan,schedule:e.target.value})} /><p className="hint">Daily at 02:00: 0 2 * * *. Sunday at 03:30: 30 3 * * 0.</p></div>
          <div className="field"><label htmlFor="backup-timezone">Timezone</label><input id="backup-timezone" required placeholder="Europe/Amsterdam" value={plan.timezone} onChange={e => setPlan({...plan,timezone:e.target.value})} /></div>
          <div className="field"><label htmlFor="backup-retention">Retention days (0 keeps indefinitely)</label><input id="backup-retention" type="number" min={0} max={36500} value={plan.retention_days} onChange={e => setPlan({...plan,retention_days:Number(e.target.value)})} /></div>
        </div><label className="row"><input type="checkbox" checked={plan.enabled} onChange={e => setPlan({...plan,enabled:e.target.checked})} />Enable schedule</label>
        <div className="form-actions"><button className="btn" disabled={busy}>Save plan</button><button type="button" className="btn secondary" onClick={() => setPlan(null)}>Cancel</button></div></fieldset>
      </form></section>}
    <h2>Destinations</h2><table aria-label="Backup destinations"><thead><tr><th>Name</th><th>Bucket / integration</th><th>Base path</th><th>Recovery key</th><th /></tr></thead><tbody>
      {destinations.map(d => <tr key={d.id}><td>{d.name}</td><td>{d.source_id || d.bucket}</td><td>{d.prefix}</td><td>{d.recovery_key_ref}</td><td>{hub.me?.acting_admin && <><button className="btn sm secondary" onClick={() => editDestination(d)}>Edit destination</button> <button className="btn sm secondary" disabled={busy} onClick={() => void act(() => api.backups.deleteDestination(d.id),"Destination removed; archives retained.")}>Remove destination</button></>}</td></tr>)}
      {!destinations.length && <tr><td colSpan={5}>No destinations configured.</td></tr>}</tbody></table>
    {destination && <section className="card" aria-label="Backup destination editor"><h2>{destinationId ? "Edit destination" : "New destination"}</h2>
      <form onSubmit={e => {e.preventDefault();void act(async () => {await api.backups.saveDestination(destination,destinationId);setDestination(null);},"Destination saved.");}}><fieldset disabled={busy}><div className="arg-grid">
        <div className="field"><label htmlFor="destination-integration">Existing S3 integration</label><select id="destination-integration" value={destination.source_id} onChange={e=>setDestination({...destination,source_id:e.target.value,bucket:"",endpoint_url:""})}><option value="">Dedicated bucket using installation workload identity</option>{integrations.filter(s=>s.kind==="s3").map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></div>
        {([['name','Name'],['bucket','Bucket (when no integration is selected)'],['prefix','Base path in bucket'],['endpoint_url','HTTPS S3 endpoint (optional)'],['region','Region'],['recovery_key_ref','Recovery key reference in the configured vault'],['kms_key_id','S3 KMS key ARN (optional)']] as const).map(([key,label]) => <div className="field" key={key}><label htmlFor={`destination-${key}`}>{label}</label><input id={`destination-${key}`} required={['name','prefix','recovery_key_ref'].includes(key)} value={destination[key]} onChange={e => setDestination({...destination,[key]:e.target.value})} /></div>)}
        <div className="field"><label htmlFor="destination-sse">S3 encryption</label><select id="destination-sse" value={destination.server_side_encryption} onChange={e => setDestination({...destination,server_side_encryption:e.target.value,kms_key_id:""})}><option value="">Bucket default</option><option value="AES256">SSE-S3</option><option value="aws:kms">SSE-KMS</option></select></div>
        <div className="field"><label htmlFor="destination-lock">Object Lock days</label><input id="destination-lock" type="number" min={0} value={destination.object_lock_days} onChange={e => setDestination({...destination,object_lock_days:Number(e.target.value)})} /></div>
      </div><p className="hint">Reference a secret that already exists in the selected vault and contains public_key. Keep private_key in a separately authorized vault secret for restore. No secret values belong in backup configuration or archives. Vault recovery is managed through that vault.</p>
      <div className="form-actions"><button className="btn">Save destination</button><button type="button" className="btn secondary" onClick={() => setDestination(null)}>Cancel</button></div></fieldset></form></section>}
    {historyPlan && <section><h2>History · {historyPlan.name}</h2><table aria-label="Backup history"><thead><tr><th>Created</th><th>Source</th><th>Status</th><th>Object</th><th>Verification</th></tr></thead><tbody>
      {history.map(r => <tr key={r.id}><td>{new Date(r.created_at).toLocaleString()}</td><td>{r.source_name || "Control plane"}</td><td>{r.status}{r.error && <p className="error-text">{r.error}</p>}</td><td>{r.object_key ? `s3://${r.bucket}/${r.object_key}` : "—"}</td><td><code>{r.sha256 || "Pending"}</code></td></tr>)}
      {!history.length && <tr><td colSpan={5}>No backups for this plan yet.</td></tr>}</tbody></table></section>}
    <p className="scope-note">Control-plane archives contain metadata and vault references. PostgreSQL uses native dumps; Iceberg v1/v2 captures metadata and retained snapshot files; S3 captures current objects with conditional reads. Other integrations require their native adapter. Restore uses the configured vault and the documented offline procedure.</p>
  </>;
}
