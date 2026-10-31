import { useEffect, useRef, useState } from "react";
import { api, AccessResource, inLayer } from "../api";
import type { Hub } from "../hub";
import type { TabRef } from "./Tabs";
import { ObjectTabs, PermissionsPanel, resourceTab } from "../components/Permissions";
import { PageHeader } from "../components/Page";
import { PublishButton } from "./Products";
import { ProductIcon } from "../components/ProductIcon";
import { ObjectLineage } from "../components/ObjectLineage";

export function KafkaClustersView({ hub, openTab }: { hub: Hub; openTab: (t: TabRef) => void }) {
  const clusters = hub.resources.filter((r) => r.kind === "kafka");
  const [registering, setRegistering] = useState(false);
  const [chosen, setLayer] = useState("");
  const free = hub.layers.filter((l) => !clusters.some((r) => r.layer_id === l.id));
  const layer = chosen || (free.some((l) => l.id === hub.layerId) ? hub.layerId : "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return <><PageHeader icon={<ProductIcon kind="kafka" size={22} />} title="Kafka" subtitle="Managed Kafka per layer. Open a cluster to browse its topics; grants on a cluster apply to all of its topics."
      actions={hub.me?.is_admin && <button className="btn secondary" onClick={() => setRegistering(!registering)}>Register Kafka</button>} />
    {registering && <form className="card" onSubmit={async (e) => {
      e.preventDefault(); setBusy(true); setError("");
      try { const result = await api.kafka.register(layer); await hub.refresh(); setRegistering(false); openTab(resourceTab(result)); }
      catch (e) { setError((e as Error).message); } finally { setBusy(false); }
    }}><h2>Register managed Kafka</h2><p className="muted">Enable Kafka in this layer’s infrastructure with OpenTofu first. Registration makes its topics available for IAM and browsing.</p>
      <label>Layer<select required value={layer} onChange={(e) => setLayer(e.target.value)}><option value="">Choose a layer</option>{free.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select></label>
      {error && <p className="error" role="alert">{error}</p>}<button className="btn" disabled={busy || !layer}>{busy ? "Registering…" : "Register"}</button></form>}
    <table><thead><tr><th>Kafka cluster</th><th>Layer</th></tr></thead><tbody>{clusters.filter((r) => inLayer(hub.layerId, r.layer_id)).map((r) => <tr key={r.id}>
      <td><button className="text-button object-name" onClick={() => openTab(resourceTab(r))}><ProductIcon kind="kafka" size={16} />{r.name}</button></td><td>{r.layer_name || "—"}</td>
    </tr>)}{!clusters.length && <tr><td colSpan={2} className="muted">No Kafka clusters you can access. Ask a cluster owner for access.</td></tr>}</tbody></table>
  </>;
}

export function KafkaView({ tab, hub, openTab }: { tab: TabRef; hub: Hub; openTab: (t: TabRef) => void }) {
  const [resource, setResource] = useState<AccessResource>();
  const [topics, setTopics] = useState<AccessResource[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [partitions, setPartitions] = useState(3);
  const [replicas, setReplicas] = useState(1);
  const [retention, setRetention] = useState(7);
  const [cleanup, setCleanup] = useState("delete");
  const [search, setSearch] = useState("");
  const revision = useRef(0);
  const section = tab.section ?? "overview";
  const load = async () => {
    const requested = ++revision.current;
    const known = hub.resources.find((r) => r.id === tab.ref);
    const r = tab.kafkaKind === 'cluster' || known?.kind === 'kafka' ? await api.kafka.cluster(tab.ref!) : await api.kafka.topic(tab.ref!);
    const children = r.kind === 'kafka' ? await api.kafka.topics(r.layer_id ?? String(r.config.layer_id)) : [];
    if (requested !== revision.current) return;
    setResource(r);
    if (r.kind === "kafka") { setTopics(children); setReplicas(Number(r.config.broker_count) >= 3 ? 3 : 1); }
    else {
      setPartitions(Number(r.config.partitions)); setReplicas(Number(r.config.replication_factor));
      setRetention(Number(r.config.retention_ms) / 86400000); setCleanup(String(r.config.cleanup_policy));
    }
  };
  useEffect(() => { let cancelled = false; setResource(undefined); setError(""); load().catch((e: Error) => { if (!cancelled) setError(e.message); }); return () => { cancelled = true; revision.current++; }; }, [tab.ref, hub.me?.active_group_id]);
  useEffect(() => {
    if (resource?.kind !== "topic" || resource.config.phase !== "Pending") return;
    const timer = window.setTimeout(() => { load().catch((e: Error) => setError(e.message)); }, 3000);
    return () => window.clearTimeout(timer);
  }, [resource]);
  const change = async (action: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await action(); await load(); await hub.refresh(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  if (!resource) return <p className={error ? "error" : "muted"}>{error || "Loading Kafka…"}</p>;
  const cluster = resource.kind === "kafka";
  const settings = { partitions, replication_factor: replicas, retention_ms: Math.round(retention * 86400000), cleanup_policy: cleanup };
  const fields = <div className="arg-grid">
    <label>Partitions<input type="number" min={Number(cluster ? 1 : resource.config.partitions)} max={10000} required value={partitions} onChange={(e) => setPartitions(Number(e.target.value))} /></label>
    <label>Replication factor<input type="number" min={1} max={5} required disabled={!cluster} value={replicas} onChange={(e) => setReplicas(Number(e.target.value))} /></label>
    <label>Retention (days)<input type="number" min={1 / 1440} max={365} step="any" required value={retention} onChange={(e) => setRetention(Number(e.target.value))} /></label>
    <label>Cleanup<select value={cleanup} onChange={(e) => setCleanup(e.target.value)}><option value="delete">Delete</option><option value="compact">Compact</option><option value="compact,delete">Compact and delete</option></select></label>
  </div>;
  return <>
    <PageHeader icon={<ProductIcon kind={resource.kind} size={22} />} title={resource.name}
      subtitle={`${cluster ? "Kafka cluster" : "Kafka topic"} · ${resource.layer_name ?? ""}${cluster ? "" : ` · ${resource.config.phase}`}`}
      actions={!cluster && <PublishButton resourceId={resource.id} openTab={openTab} hub={hub} />} />
    {resource.owner_group_name && <p className="hint">Owner group · <strong>{resource.owner_group_name}</strong></p>}
    <ObjectTabs active={section} lineage settings={!cluster && resource.actions.includes("ddl")} onChange={(s) => openTab({ ...tab, section: s })} />
    {error && <p className="error" role="alert">{error}</p>}
    {section === "permissions" ? <PermissionsPanel resource={resource} resources={[...hub.resources, ...topics, resource]} openTab={openTab} onChanged={async () => { await load(); await hub.refresh(); }} />
      : section === "lineage" ? <ObjectLineage resourceId={resource.id} openTab={openTab} />
      : section === "settings" && !cluster ? <form className="card" onSubmit={(e) => { e.preventDefault(); void change(() => api.kafka.update(resource.id, settings)); }}>
        {fields}<p className="muted">Partitions can only increase. Replica reassignment requires a controlled broker operation.</p>
        <button className="btn" disabled={busy}>Save settings</button>{" "}
        <button type="button" className="btn danger" disabled={busy} onClick={() => { if (confirm(`Delete topic ${resource.name} and its messages?`)) void change(async () => { await api.kafka.remove(resource.id); openTab({ kind: "kafka", ref: resource.parent_id!, title: "Kafka" }); }); }}>Delete topic</button>
      </form> : cluster ? <>
        <div className="object-toolbar"><input type="search" placeholder="Find a topic…" aria-label="Find a topic" value={search} onChange={(e) => setSearch(e.target.value)} />
          <button className="btn secondary" disabled={busy} onClick={() => void change(async () => {})}>Refresh</button>
          {resource.actions.includes("ddl") && <button className="btn" onClick={() => setAdding(!adding)}>Create topic</button>}</div>
        {adding && <form className="card" onSubmit={(e) => { e.preventDefault(); void change(async () => { const topic = await api.kafka.create({ ...settings, name, layer_id: resource.layer_id ?? resource.config.layer_id }); setAdding(false); openTab(resourceTab(topic)); }); }}>
          <label>Topic name<input required maxLength={249} pattern="[A-Za-z0-9][A-Za-z0-9._-]*" value={name} onChange={(e) => setName(e.target.value)} /></label>{fields}<button className="btn" disabled={busy}>Create</button></form>}
        <table><thead><tr><th>Topic</th><th>Status</th><th>Partitions</th><th>Replicas</th><th /></tr></thead><tbody>
          {topics.filter((t) => t.name.toLowerCase().includes(search.toLowerCase())).map((t) => <tr key={t.id}><td><button className="text-button object-name" onClick={() => openTab(resourceTab(t))}><ProductIcon kind="topic" size={16} />{t.name}</button></td><td>{String(t.config.phase)}</td><td>{String(t.config.partitions)}</td><td>{String(t.config.replication_factor)}</td><td><button className="btn sm secondary" onClick={() => openTab(resourceTab(t, "permissions"))}>Permissions</button></td></tr>)}
          {!topics.length && <tr><td colSpan={5}>No topics accessible to your active group.</td></tr>}
        </tbody></table>
      </> : <section className="card"><h2>Connect from a workload</h2><pre className="code">{`import fdp\n\nproducer = fdp.kafka.producer(${JSON.stringify(resource.name)}, layer_id=${JSON.stringify(resource.layer_id)})\nproducer.send({"event": "example"}).get(timeout=30)\nproducer.close()\n\nconsumer = fdp.kafka.consumer(${JSON.stringify(resource.name)}, group="my-program", layer_id=${JSON.stringify(resource.layer_id)})\nfor record in consumer:\n    process(record.value)\n    consumer.commit()`}</pre><p className="muted">Install fdp[kafka]. Tokens refresh automatically using your execution identity. Consumer groups are isolated by identity; commits are explicit.</p></section>}
  </>;
}
