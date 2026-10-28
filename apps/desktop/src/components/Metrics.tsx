import { useEffect, useState } from 'react';
import { api, CatalogAsset, MetricDefinition, MetricObservation, SlaIncident } from '../api';
import type {OpenTab} from './Permissions';

const label = (v: string) => v.replaceAll('_', ' ');
const Badge = ({value}: {value:string}) => <span className={`badge catalog-status ${value}`}>{label(value)}</span>;

export function CatalogAlerts({openTab}:{openTab:OpenTab}) {
  const [items,setItems]=useState<SlaIncident[]>([]);
  useEffect(()=>{let active=true;api.catalog.alerts().then(v=>{if(active)setItems(v);}).catch(()=>{});return()=>{active=false;};},[]);
  if(!items.length)return null;
  return <section className="card" aria-label="Active SLA alerts"><h2>Data needs attention</h2><ul className="home-list">{items.slice(0,5).map(item=><li key={item.id}><button className="text-button" onClick={()=>openTab({kind:'catalog-object',ref:item.asset_id,title:item.asset_name||'Data product',catalogSection:'sla'})}>{item.asset_name||'Data product'}</button><Badge value={item.status}/></li>)}</ul>{items.length>5&&<p className="hint">{items.length} active alerts. Open a product’s SLA tab for details.</p>}</section>;
}

export function MetricsPanel({asset}: {asset:CatalogAsset}) {
  const [items,setItems]=useState<MetricDefinition[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [draft,setDraft]=useState<MetricDefinition|null|undefined>(undefined);
  const reload=()=>api.catalog.metrics(asset.id).then(setItems).catch((e:Error)=>setError(e.message));
  useEffect(()=>{let alive=true;api.catalog.metrics(asset.id).then(v=>{if(alive)setItems(v);}).catch((e:Error)=>{if(alive)setError(e.message);});return()=>{alive=false;};},[asset.id]);
  return <section aria-label="KPIs and data quality">
    <div className="section-heading"><div><h2>KPIs & data quality</h2><p className="muted">Reusable definitions, measured values and history. Definitions and dependencies retain this product’s IAM and layer.</p></div>
      <div className="row"><button className="btn secondary" onClick={reload}>Refresh measurements</button>{asset.can_govern && <button className="btn" onClick={()=>setDraft(null)}>Add definition</button>}</div></div>
    {error&&<p className="error" role="alert">{error}</p>}
    {draft!==undefined&&<MetricForm key={draft?.id||'new'} asset={asset} metric={draft} onCancel={()=>setDraft(undefined)} onSaved={async()=>{setDraft(undefined);await reload();}}/>}
    {!items.length&&<p className="muted">No definitions yet. dbt tests appear automatically after their results are imported.</p>}
    {items.map(metric=><section className="card" key={metric.id}>
      <div className="section-heading" style={{marginTop:0}}><h3>{metric.name} <Badge value={metric.latest?.status||'unknown'}/></h3>
        <span className="badge">{metric.kind==='kpi'?'Business KPI':'Data quality'} · {metric.origin==='dbt'?'Managed by dbt':`Version ${metric.version}`}{!metric.enabled?' · Disabled':''}</span></div>
      <p>{metric.description||'No description.'}</p>
      <p style={{fontSize:24,margin:'8px 0'}}><strong>{metric.latest?.value??'—'}</strong> {metric.unit}</p>
      <p className="hint">{metric.latest?`${new Date(metric.latest.observed_at).toLocaleString()} · ${metric.latest.origin} · definition v${metric.latest.definition_version}`:'Not measured yet.'}</p>
      {metric.latest&&metric.latest.definition_version!==metric.version&&<p className="scope-note">The definition changed since this measurement. Measure again before relying on its status.</p>}
      <div className="form-actions">
        {!!metric.definition.sql&&<button className="btn secondary" disabled={busy||!metric.enabled} onClick={async()=>{setBusy(true);setError('');try{await api.catalog.measureMetric(asset.id,metric.id);await reload();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>Measure {metric.name}</button>}
        {asset.can_govern&&metric.origin==='manual'&&<button className="btn secondary" onClick={()=>setDraft(metric)}>Edit {metric.name}</button>}
      </div>
      <MeasurementHistory asset={asset} metric={metric}/>
    </section>)}
  </section>;
}

function MetricForm({asset,metric,onCancel,onSaved}:{asset:CatalogAsset;metric:MetricDefinition|null;onCancel:()=>void;onSaved:()=>Promise<void>}) {
  const [form,setForm]=useState({key:metric?.key||'',name:metric?.name||'',description:metric?.description||'',kind:metric?.kind||'kpi',unit:metric?.unit||'',source:metric?.definition.source||String(asset.attributes.source||''),sql:metric?.definition.sql||'',dependencies:metric?.definition.dependencies?.filter(id=>id!==asset.id).join(', ')||'',enabled:metric?.enabled??true});
  const [bounds,setBounds]=useState<Record<string,string>>(Object.fromEntries(['minimum','maximum','warn_minimum','warn_maximum'].map(k=>[k,String(metric?.definition[k as keyof MetricDefinition['definition']]??'')])));
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  return <form className="card" aria-label="Metric definition" onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');try{await api.catalog.defineMetric(asset.id,{...form,source:form.sql.trim()?form.source:'',dependencies:form.dependencies.split(',').map(v=>v.trim()).filter(Boolean),...Object.fromEntries(Object.entries(bounds).map(([k,v])=>[k,v===''?null:Number(v)]))});await onSaved();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>
    <h3>{metric?'Edit definition':'New definition'}</h3>
    <div className="arg-grid"><div className="field"><label htmlFor="metric-key">Definition key</label><input id="metric-key" required disabled={!!metric} pattern="[a-zA-Z0-9_.-]+" value={form.key} onChange={e=>setForm({...form,key:e.target.value})}/></div>
      <div className="field"><label htmlFor="metric-name">Name</label><input id="metric-name" required value={form.name} onChange={e=>setForm({...form,name:e.target.value})}/></div>
      <div className="field"><label htmlFor="metric-kind">Type</label><select id="metric-kind" value={form.kind} onChange={e=>setForm({...form,kind:e.target.value as 'kpi'|'quality'})}><option value="kpi">Business KPI</option><option value="quality">Data quality</option></select></div>
      <div className="field"><label htmlFor="metric-unit">Unit</label><input id="metric-unit" placeholder="%, cards, EUR…" value={form.unit} onChange={e=>setForm({...form,unit:e.target.value})}/></div></div>
    <div className="field"><label htmlFor="metric-description">Description</label><textarea id="metric-description" rows={2} value={form.description} onChange={e=>setForm({...form,description:e.target.value})}/></div>
    <div className="arg-grid">{(['minimum','maximum','warn_minimum','warn_maximum'] as const).map(k=><div className="field" key={k}><label htmlFor={'metric-'+k}>{({minimum:'Failure below',maximum:'Failure above',warn_minimum:'Warning below',warn_maximum:'Warning above'})[k]}</label><input id={'metric-'+k} type="number" step="any" value={bounds[k]} onChange={e=>setBounds({...bounds,[k]:e.target.value})}/></div>)}</div>
    <div className="field"><label htmlFor="metric-source">SQL source</label><input id="metric-source" placeholder="pokemon-test" value={form.source} onChange={e=>setForm({...form,source:e.target.value})}/></div>
    <div className="field"><label htmlFor="metric-sql">SQL calculation (optional)</label><textarea id="metric-sql" rows={4} className="mono" placeholder="SELECT count(*) FROM pokemon_test.analytics.dim_card" value={form.sql} onChange={e=>setForm({...form,sql:e.target.value})}/><span className="hint">One read-only query returning one numeric value. Runs through Trino with your permissions, narrowed to declared dependency schemas.</span></div>
    <div className="field"><label htmlFor="metric-dependencies">Additional dependency catalog IDs</label><input id="metric-dependencies" value={form.dependencies} onChange={e=>setForm({...form,dependencies:e.target.value})}/><span className="hint">Comma separated. This product is included automatically. All dependencies must be in the same layer and visible to metric readers.</span></div>
    <label className="row"><input type="checkbox" checked={form.enabled} onChange={e=>setForm({...form,enabled:e.target.checked})}/>Enabled</label>
    {error&&<p className="error" role="alert">{error}</p>}
    <div className="form-actions"><button className="btn" disabled={busy}>Save definition</button><button type="button" className="btn secondary" onClick={onCancel}>Cancel</button></div>
  </form>;
}

function MeasurementHistory({asset,metric}:{asset:CatalogAsset;metric:MetricDefinition}) {
  const [rows,setRows]=useState<MetricObservation[]|null>(null),[error,setError]=useState('');
  return <details onToggle={e=>{if(e.currentTarget.open)api.catalog.metricHistory(asset.id,metric.id).then(setRows).catch((e:Error)=>setError(e.message));}}><summary>Measurement history</summary>
    {error&&<p role="alert" className="error">{error}</p>}
    {rows&&<><MetricTrend rows={rows}/><div className="table-scroll"><table aria-label={`History of ${metric.name}`}><thead><tr><th>Time</th><th>Value</th><th>Status</th><th>Definition</th><th>Origin / revision</th></tr></thead><tbody>
      {rows.map(o=><tr key={o.id}><td>{new Date(o.observed_at).toLocaleString()}</td><td>{o.value??'Unknown'} {metric.unit}</td><td><Badge value={o.status}/></td><td>v{o.definition_version}</td><td>{o.origin}{o.revision&&` · ${o.revision.slice(0,12)}`}{o.run_id&&` · run ${o.run_id}`}</td></tr>)}
      {!rows.length&&<tr><td colSpan={5}>No measurements yet.</td></tr>}
    </tbody></table></div></>}
  </details>;
}

function MetricTrend({rows}:{rows:MetricObservation[]}) {
  const points=rows.filter(r=>r.value!==null).slice(0,40).reverse();
  if(points.length<2)return null;
  const min=Math.min(...points.map(r=>r.value!)),max=Math.max(...points.map(r=>r.value!));
  return <svg viewBox="0 0 500 80" role="img" aria-label="Recent measurement trend, oldest to newest" style={{width:'100%',maxWidth:600,height:80}}><polyline fill="none" stroke="currentColor" strokeWidth="2" points={points.map((r,i)=>`${5+i*490/(points.length-1)},${70-60*(r.value!-min)/(max-min||1)}`).join(' ')}/></svg>;
}

export function SlaHistory({asset,onEvaluated}:{asset:CatalogAsset;onEvaluated?:()=>Promise<void>}) {
  const [events,setEvents]=useState<{id:string;evaluated_at:string;status:string}[]>([]),[alerts,setAlerts]=useState<SlaIncident[]>([]),[error,setError]=useState('');
  const load=async()=>{const [history,incidents]=await Promise.all([api.catalog.slaHistory(asset.id),api.catalog.alerts(asset.id)]);setEvents(history);setAlerts(incidents);};
  useEffect(()=>{load().catch((e:Error)=>setError(e.message));},[asset.id,asset.sla]);
  return <section className="card"><div className="section-heading" style={{marginTop:0}}><h2>Alerts & history</h2><button className="btn secondary" onClick={async()=>{try{await api.catalog.evaluateSla(asset.id);await load();await onEvaluated?.();}catch(e){setError((e as Error).message);}}}>Evaluate SLA now</button></div>
    <p className="muted">Evaluated in the background independently of Airflow. Acknowledgement records ownership of an alert; it does not resolve the breach or bypass an input gate.</p>
    {error&&<p className="error" role="alert">{error}</p>}
    {alerts.map(a=><div className="scope-note" key={a.id}><Badge value={a.status}/> Since {new Date(a.opened_at).toLocaleString()}
      {a.acknowledged_at?' · Acknowledged':asset.can_govern&&<button className="text-button" style={{marginLeft:12}} onClick={async()=>{try{await api.catalog.acknowledgeAlert(a.id);await load();}catch(e){setError((e as Error).message);}}}>Acknowledge alert</button>}</div>)}
    <table aria-label="SLA history"><thead><tr><th>Evaluated</th><th>Status</th></tr></thead><tbody>{events.map(e=><tr key={e.id}><td>{new Date(e.evaluated_at).toLocaleString()}</td><td><Badge value={e.status}/></td></tr>)}{!events.length&&<tr><td colSpan={2}>No SLA evaluations recorded yet.</td></tr>}</tbody></table>
  </section>;
}
