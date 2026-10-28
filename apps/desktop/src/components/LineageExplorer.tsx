import { containerMembers, focusAncestors, projectLineage } from "./lineageProjection";
import { objectKindLabel } from "../objectKinds";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, useNodesState, type Node, type NodeProps, type ReactFlowInstance } from "@xyflow/react";
import * as dagre from "@dagrejs/dagre";
import "@xyflow/react/dist/style.css";
import { api, type CatalogAsset, type CatalogGraph } from "../api";
import type { TabRef } from "../views/Tabs";
import { ProductIcon } from "./ProductIcon";
import { IconClose } from "./Icons";

type Entity = CatalogGraph["nodes"][number];
type Link = CatalogGraph["edges"][number];
type NodeData = { entity: Entity; focused: boolean; columns: boolean; group: boolean; compact: boolean; expanded: Set<string>; containers: Set<string>; toggleContainer: (id: string) => void; expand: (id: string, direction: string) => void };
type FlowNode = Node<NodeData, "entity">;
const words = (value: string) => value.replaceAll("_", " ");
const kind = (a: CatalogAsset) => a.kind.startsWith("airflow") ? "airflow" : a.kind.startsWith("dbt") ? "dbt" : a.kind === "schema" && a.attributes.source_kind === "s3" ? "folder" : a.kind === "source" ? a.attributes.source_kind : a.kind === "program" ? "python" : a.kind;
/** Above this many objects nodes start compact, so the structure stays readable. */
const COMPACT_AFTER = 20;
const color = (origin: string) => origin.startsWith("dbt") ? "#d88e71" : origin.startsWith("airflow") ? "#51b9a0" : origin === "trino_runtime" ? "#b593d6" : "#6aa8d8";

function ExpandButtons({ data, compact }: { data: NodeData; compact: boolean }) {
  const a = data.entity;
  return <div className={`lineage-expand nodrag nopan ${compact ? "compact" : ""}`}>{([['upstream', a.upstream_count], ['downstream', a.downstream_count]] as const).map(([dir, count]) => {
    const open = data.expanded.has(`${a.id}:${dir}`);
    return <button key={dir} disabled={count === 0} aria-label={`${open ? "Collapse" : "Expand"} ${dir} of ${a.name}`} title={`${open ? "Collapse" : "Expand"} ${dir}${count !== undefined ? ` (${count})` : ""}`}
      className={open ? "open" : ""} onClick={(e) => { e.stopPropagation(); data.expand(a.id, dir); }}>
      {dir === "upstream" ? "←" : "→"}{compact ? count !== undefined && ` ${count}` : <> {open ? "Collapse" : words(dir)}{count !== undefined && ` (${count})`}</>}</button>;
  })}</div>;
}

function ContainerButton({ data }: { data: NodeData }) {
  if (!data.entity.lineage_child_count) return null;
  const open = data.containers.has(data.entity.id);
  return <button className="lineage-container-toggle nodrag nopan" aria-label={`${open ? "Collapse" : "Expand"} contents of ${data.entity.name}`}
    aria-expanded={open} onClick={event => { event.stopPropagation(); data.toggleContainer(data.entity.id); }}>
    {open ? "− Collapse contents" : `+ Expand contents (${data.entity.lineage_child_count})`}
  </button>;
}

function EntityNode({ data }: NodeProps<FlowNode>) {
  const a = data.entity;
  if (data.compact && !data.group) return <div className={`lineage-entity compact ${data.focused ? "focused" : ""}`}>
    <Handle type="target" position={Position.Left} />
    <div className="lineage-entity-title"><ProductIcon kind={kind(a)} runtime={a.attributes.runtime} size={16} /><strong title={a.name}>{a.name}</strong><ExpandButtons data={data} compact /></div>
    <ContainerButton data={data} />
    <div className="muted">{objectKindLabel(a.kind, a.attributes.source_kind)}{a.kind === "schema" && !!a.attributes.source && ` · ${String(a.attributes.source)}`}{a.attributes.branch && ` · ${a.attributes.branch}`}</div>
    <Handle type="source" position={Position.Right} />
  </div>;
  return <div className={`lineage-entity ${data.focused ? "focused" : ""} ${data.group ? "pipeline" : ""}`} style={data.group ? { borderColor: a.kind === "dbt" ? "#d88e71" : a.kind === "airflow_dag" ? "#51b9a0" : "#91b9c8" } : undefined}>
    <Handle type="target" position={Position.Left} />
    <div className="lineage-entity-title"><ProductIcon kind={kind(a)} runtime={a.attributes.runtime} /><strong title={a.name}>{a.name}</strong>{data.group && <span className="badge">{a.kind === "airflow_dag" ? "Pipeline" : a.kind === "dbt" ? "Project" : "Container"}</span>}</div>
    <div className="muted">{objectKindLabel(a.kind, a.attributes.source_kind)}{a.kind === "schema" && !!a.attributes.source && ` · ${String(a.attributes.source)}`}{a.attributes.branch && ` · ${a.attributes.branch}`}</div>
    {!data.group && <>
      <div className="lineage-entity-meta"><span>{a.governance.domain || a.resource.name}</span><span className={`catalog-status ${a.service_level.status}`}>{words(a.service_level.status)}</span></div>
      {data.columns && !!a.attributes.columns?.length && <div className="lineage-columns">{a.attributes.columns.slice(0, 8).map((col) => <div key={col.name}><span>{col.name}</span><span className="muted">{col.data_type}</span></div>)}{a.attributes.columns.length > 8 && <span className="muted">+{a.attributes.columns.length - 8} columns · inspect for all</span>}</div>}
    </>}
    <ContainerButton data={data} />
    <ExpandButtons data={data} compact={false} />
    <Handle type="source" position={Position.Right} />
  </div>;
}
const nodeTypes = { entity: EntityNode };

function merge(base: CatalogGraph | null, expansions: Record<string, CatalogGraph>): CatalogGraph {
  const nodes = new Map<string, Entity>(), edges = new Map<string, Link>();
  let truncated = false;
  for (const graph of [base, ...Object.values(expansions)]) {
    if (!graph) continue;
    truncated ||= graph.truncated;
    for (const a of graph.nodes) if (nodes.has(a.id) || nodes.size < 150) nodes.set(a.id, nodes.has(a.id) ? { ...a, distance: nodes.get(a.id)!.distance } : a); else truncated = true;
    for (const e of graph.edges) if (edges.has(e.id) || edges.size < 400) edges.set(e.id, e); else truncated = true;
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()].filter((e) => nodes.has(e.source) && nodes.has(e.target)), truncated };
}

function layout(graph: CatalogGraph, focus: string, columns: boolean, compact: boolean, expanded: Set<string>, expand: NodeData["expand"], containers: Set<string>, toggleContainer: NodeData["toggleContainer"]) {
  const W = compact ? 230 : 268;
  const byId = new Map(graph.nodes.map(node => [node.id, node]));
  const groups = new Map<string, string>();
  for (const node of graph.nodes) {
    if (node.lineage_parent_id && byId.has(node.lineage_parent_id)) groups.set(node.id, node.lineage_parent_id);
    else if (node.kind === "airflow_task") {
      const parent = graph.nodes.find(a => a.kind === "airflow_dag" && a.resource.id === node.resource.id && a.attributes.dag_id === node.attributes.dag_id);
      if (parent) groups.set(node.id, parent.id);
    }
  }
  const parents = new Set(groups.values());
  const height = (a: Entity) => (compact && !parents.has(a.id) ? 58 : 128 + (columns && !parents.has(a.id) && a.attributes.columns?.length ? 10 + Math.min(8, a.attributes.columns.length) * 22 : 0)) + (a.lineage_child_count ? 30 : 0);
  const sizes = new Map<string, { width: number; height: number }>();
  const positions = new Map<string, { x: number; y: number }>();
  const children = new Map<string | undefined, Entity[]>();
  for (const node of graph.nodes) children.set(groups.get(node.id), [...(children.get(groups.get(node.id)) || []), node]);
  // Each nested container lays out its immediate children after measuring descendants.
  function level(parent?: string): { width: number; height: number } {
    const siblings = children.get(parent) || [];
    const g = new dagre.graphlib.Graph().setGraph({ rankdir: "LR", nodesep: compact ? 24 : 40, ranksep: parent ? 70 : 105, marginx: 28, marginy: 20 }).setDefaultEdgeLabel(() => ({}));
    for (const a of siblings) {
      const size = parents.has(a.id) ? level(a.id) : { width: W, height: height(a) };
      sizes.set(a.id, size); g.setNode(a.id, size);
    }
    function atLevel(id: string): string | undefined {
      const seen = new Set<string>();
      while (groups.get(id) !== parent) {
        if (seen.has(id) || !groups.has(id)) return undefined;
        seen.add(id); id = groups.get(id)!;
      }
      return id;
    }
    for (const edge of graph.edges) {
      const source = atLevel(edge.source), target = atLevel(edge.target);
      if (source && target && source !== target) g.setEdge(source, target);
    }
    dagre.layout(g);
    for (const a of siblings) {
      const size = sizes.get(a.id)!;
      positions.set(a.id, { x: g.node(a.id).x - size.width / 2, y: g.node(a.id).y - size.height / 2 + (parent ? 140 : 0) });
    }
    return { width: Math.max(324, g.graph().width || W), height: (g.graph().height || 128) + 156 };
  }
  level();
  function depth(id: string) {
    let value = 0; const seen = new Set<string>();
    while (groups.has(id) && !seen.has(id)) { seen.add(id); id = groups.get(id)!; value++; }
    return value;
  }
  const nodes: FlowNode[] = [...graph.nodes].sort((a,b) => depth(a.id) - depth(b.id)).map(a => {
    const parentId = groups.get(a.id), size = sizes.get(a.id)!;
    return { id: a.id, type: "entity", data: { entity: a, focused: a.id === focus, columns, group: parents.has(a.id), compact, expanded, expand, containers, toggleContainer },
      position: positions.get(a.id)!, ...(parentId ? { parentId, extent: "parent" as const } : {}),
      style: { width: size.width, ...(parents.has(a.id) ? { height: size.height } : {}) },
      ariaLabel: `${a.name}, ${words(a.kind)}`, dragHandle: ".lineage-entity-title" };
  });
  const edges = graph.edges.filter(e => !(e.relation === "contains" && groups.get(e.target) === e.source)).map(e => ({
    id: e.id, source: e.source, target: e.target,
    label: (e.category === "operational" ? `${words(e.relation)} · operational` : e.category === "schema" ? "Creates schema object" : words(e.relation)) + ((e.collapsed_count || 0) > 1 ? ` · ${e.collapsed_count} connections` : ""),
    type: "smoothstep", data: { evidence: e }, zIndex: 0,
    style: { stroke: e.category === "operational" || e.category === "schema" ? "#64748b" : color(e.origin), strokeWidth: 1.6, ...((e.control || e.relation === "contains") ? { strokeDasharray: "5 4" } : {}) },
    labelStyle: { fill: "var(--fg-muted)", fontSize: 10 }, labelBgStyle: { fill: "var(--bg)" },
    markerEnd: { type: MarkerType.ArrowClosed, color: e.category === "operational" || e.category === "schema" ? "#64748b" : color(e.origin) }, ariaLabel: `${words(e.relation)}, ${words(e.origin)}`,
  }));
  return { nodes, edges };
}

export function LineageExplorer({ asset, openTab, version }: { asset: CatalogAsset; openTab: (tab: TabRef) => void; version: number }) {
  const [focus, setFocus] = useState(asset.id), [direction, setDirection] = useState("upstream"), [depth, setDepth] = useState("16"), [origin, setOrigin] = useState(""), [windowDays, setWindowDays] = useState("");
  const view = "flow";
  const [orchestration, setOrchestration] = useState(false), [operational, setOperational] = useState(false), [filtersOpen, setFiltersOpen] = useState(false);
  const [containers, setContainers] = useState<Set<string>>(new Set());
  const [contents, setContents] = useState<Record<string, CatalogGraph>>({});
  const [base, setBase] = useState<CatalogGraph | null>(null), [expansions, setExpansions] = useState<Record<string, CatalogGraph>>({});
  const [selected, setSelected] = useState<string | null>(null), [connection, setConnection] = useState<Link | null>(null), [columns, setColumns] = useState(false), [mode, setMode] = useState("explore"), [find, setFind] = useState("");
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null), [compactPref, setCompactPref] = useState<boolean | null>(null);
  const viewCenter = useRef(focus);
  const fitTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const jumpTo = (ids: string[]) => { clearTimeout(fitTimer.current); viewCenter.current = ids[0] || focus; void instance.current?.fitView({ nodes: ids.map(id => ({ id })), padding: .3, maxZoom: 1, duration: 0 }); };
  const generation = useRef(0), pending = useRef(new Set<string>()), instance = useRef<ReactFlowInstance<FlowNode> | null>(null);
  const filters = useMemo(() => ({ view, hierarchy: "true", direction, depth, origin, orchestration: String(orchestration), operational: String(operational), ...(windowDays ? { since: new Date(Date.now() - Number(windowDays) * 86400000).toISOString() } : {}) }), [view, direction, depth, origin, orchestration, operational, windowDays, version]);
  useEffect(() => {
    viewCenter.current = focus;
    const current = ++generation.current; pending.current.clear(); setExpansions({}); setContents({}); setContainers(new Set()); setBase(null); setSelected(null); setConnection(null); setBusy(true); setError("");
    api.catalog.lineage(focus, filters).then((g) => { if (generation.current === current) setBase(g); }).catch((e: Error) => { if (generation.current === current) setError(e.message); }).finally(() => { if (generation.current === current) setBusy(false); });
    return () => { generation.current++; };
  }, [focus, filters, version]);
  const expand = useCallback(async (id: string, dir: string) => {
    const key = `${id}:${dir}`, current = generation.current;
    if (pending.current.has(key)) return;
    if (expansions[key]) { setExpansions((values) => { const next = { ...values }; delete next[key]; return next; }); return; }
    pending.current.add(key); setError("");
    try { const g = await api.catalog.lineage(id, { ...filters, direction: dir, depth: "1" }); if (current === generation.current) setExpansions((v) => ({ ...v, [key]: g })); }
    catch (e) { if (current === generation.current) setError((e as Error).message); }
    finally { pending.current.delete(key); }
  }, [expansions, filters]);
  const raw = useMemo(() => merge(base, { ...expansions, ...Object.fromEntries(Object.entries(contents).map(([key,value]) => ["contents:" + key,value])) }), [base, expansions, contents]);
  const autoOpen = useMemo(() => focusAncestors(raw, focus), [raw, focus]);
  const openContainers = useMemo(() => new Set([...autoOpen, ...containers]), [autoOpen, containers]);
  const toggleContainer = useCallback(async (id: string) => {
    viewCenter.current = id;
    if (openContainers.has(id)) {
      setContainers(previous => { const next = new Set(previous); next.delete(id); return next; });
      if (autoOpen.has(id)) setFocus(id);
      return;
    }
    const soleDag = raw.nodes.filter(node => node.lineage_parent_id === id && node.kind === "airflow_dag");
    setContainers(previous => new Set([...previous, id, ...(soleDag.length === 1 ? [soleDag[0].id] : [])]));
    const key = `contents:${id}`, current = generation.current;
    if (contents[id] || pending.current.has(key)) return;
    pending.current.add(key); setError("");
    try {
      const details = await api.catalog.lineage(id, { ...filters, direction: "upstream", depth: "16" });
      if (generation.current === current) setContents(previous => ({ ...previous, [id]: details }));
    } catch (reason) { if (generation.current === current) setError((reason as Error).message); }
    finally { pending.current.delete(key); }
  }, [openContainers, autoOpen, contents, filters, raw]);
  const graph = useMemo(() => projectLineage(raw, openContainers), [raw, openContainers]);
  const compact = !columns && (compactPref ?? graph.nodes.length > COMPACT_AFTER);
  const arranged = useMemo(() => layout(graph, focus, columns, compact, new Set(Object.keys(expansions)), expand, openContainers, toggleContainer), [graph, focus, columns, compact, expansions, expand, openContainers, toggleContainer]);
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>([]);
  useEffect(() => {
    // Keep measured dimensions for mounted nodes. Dropping them on a content refresh
    // clears React Flow's handle bounds without necessarily triggering a resize.
    setNodes(previous => {
      const measured = new Map(previous.map(node => [node.id, node.measured]));
      return arranged.nodes.map(node => ({ ...node, measured: measured.get(node.id) }));
    });
    // Fitting a large graph makes every node unreadable: start at the centered object and its direct
    // neighbours instead, at a legible zoom. Pan, the minimap, and Find reach the rest.
    const center = graph.nodes.some(node => node.id === viewCenter.current) ? viewCenter.current : focus;
    const members = new Set([center, ...graph.nodes.filter((a) => a.lineage_parent_id === center).map((a) => a.id)]);
    const near = view === "flow" || graph.nodes.length > COMPACT_AFTER
      ? [center, ...graph.edges.filter((e) => members.has(e.source) !== members.has(e.target)).map((e) => members.has(e.source) ? e.target : e.source)]
      : null;
    fitTimer.current = setTimeout(() => instance.current?.fitView(near
      ? { nodes: near.map((id) => ({ id })), padding: .3, duration: 250, maxZoom: 1, minZoom: members.size > 1 ? .3 : view === "flow" ? .85 : .7 }
      : { padding: .15, duration: 250, maxZoom: 1 }), 80);
    return () => clearTimeout(fitTimer.current);
  }, [arranged, setNodes]); // eslint-disable-line react-hooks/exhaustive-deps
  // Trace the full upstream and downstream path of the selected (or hovered) object; dim the rest.
  const highlight = selected ?? hovered;
  const path = useMemo(() => {
    if (!highlight || !graph.nodes.some((n) => n.id === highlight)) return null;
    const roots = containerMembers(graph, highlight);
    const ids = new Set(roots), edgeIds = new Set<string>();
    for (const forward of [true, false]) {
      const queue = [...roots], seen = new Set(queue);
      for (let i = 0; i < queue.length; i++) for (const e of graph.edges) {
        const [from, to] = forward ? [e.source, e.target] : [e.target, e.source];
        if (from !== queue[i]) continue;
        edgeIds.add(e.id); ids.add(to);
        if (!seen.has(to)) { seen.add(to); queue.push(to); }
      }
    }
    return { ids, edgeIds };
  }, [highlight, graph, arranged.nodes]);
  useEffect(() => {
    const keep = path && new Set([...path.ids, ...[...path.ids].flatMap(id => [...focusAncestors(graph, id)])]);
    setNodes((current) => current.map((n) => ({ ...n, className: keep && !keep.has(n.id) ? "dimmed" : keep ? "traced" : "" })));
  }, [path, graph, arranged, setNodes]);
  const edges = useMemo(() => arranged.edges.map((e) => {
    const on = path?.edgeIds.has(e.id);
    // Labels crowd large graphs: show them on the traced path, or everywhere when the graph is small.
    const label = e.data.evidence.control || on || (!path && arranged.edges.length <= 12) ? e.label : undefined;
    return { ...e, label, zIndex: on ? 1 : 0, animated: !!on,
      style: { ...e.style, strokeWidth: on ? 2.4 : 1.6, opacity: path && !on ? .12 : 1 } };
  }), [arranged.edges, path]);
  const stages = useMemo(() => {
    const transforms = new Set(graph.nodes.filter((a) => ["dbt", "dbt_model", "dbt_seed", "dbt_snapshot"].includes(a.kind)).map((a) => a.id));
    const outputs = new Set(graph.edges.filter((e) => e.relation === "writes" && transforms.has(e.source)).map((e) => e.target));
    const data = (a: Entity) => ["table", "view", "materialized_view", "file", "schema"].includes(a.kind) && a.id !== focus;
    return [
      { label: "APIs", ids: graph.nodes.filter((a) => a.kind === "api" && a.id !== focus).map((a) => a.id) },
      { label: "Ingestion", ids: graph.nodes.filter((a) => ["script", "program", "notebook"].includes(a.kind) && a.id !== focus).map((a) => a.id) },
      { label: "Input data", ids: graph.nodes.filter((a) => data(a) && !outputs.has(a.id)).map((a) => a.id) },
      { label: "dbt", ids: [...transforms].filter((id) => id !== focus) },
      { label: "Output data", ids: graph.nodes.filter((a) => data(a) && outputs.has(a.id)).map((a) => a.id) },
      { label: "Inspected object", ids: [focus] },
    ].filter((stage) => stage.ids.length);
  }, [graph, focus]);
  const selectedAsset = graph.nodes.find((n) => n.id === selected), current = graph.nodes.find((n) => n.id === focus);
  const open = (a: CatalogAsset) => openTab({ kind: "catalog-object", ref: a.id, title: a.name, catalogSection: "lineage" });
  const choose = (id: string) => { setSelected(id); setConnection(null); };
  const impact = useMemo(() => {
    // Directed reachability, independent of the graph's display-layer distance.
    const members = containerMembers(graph, focus).filter(id => id !== focus);
    const distance = new Map([focus, ...members].map((id) => [id, 0])), queue = [focus, ...members];
    for (let i = 0; i < queue.length; i++) for (const e of graph.edges) if (!e.control && e.relation !== "contains" && e.source === queue[i] && !distance.has(e.target)) { distance.set(e.target, distance.get(queue[i])! + 1); queue.push(e.target); }
    return graph.nodes.filter((a) => a.id !== focus && !members.includes(a.id) && distance.has(a.id)).map((a) => ({ ...a, hops: distance.get(a.id)! })).sort((a, b) => a.hops - b.hops || a.name.localeCompare(b.name));
  }, [graph, focus]);
  return <>
    <div className="object-toolbar" style={{ marginTop: 0 }}><div className="segmented" role="group" aria-label="Lineage scope">
      <button aria-pressed={direction === "upstream"} onClick={() => { setDirection("upstream"); setDepth("16"); setMode("explore"); setFiltersOpen(false); }}>Upstream lineage</button>
      <button aria-pressed={direction === "both"} onClick={() => { setDirection("both"); setDepth("16"); setMode("explore"); setFiltersOpen(true); }}>Upstream &amp; downstream</button>
      <button aria-pressed={direction === "downstream"} onClick={() => { setDirection("downstream"); setDepth("16"); setMode("explore"); }}>Downstream lineage</button>
    </div></div>
    {view === "flow" && <p className="hint">Follow data from its API or source through ingestion and dbt models to this object. Only paths into or out of this object are included. Projects, schemas and Airflow programs start collapsed. Expand contents to reveal models, tables or tasks; connections follow the visible level. Show orchestration for Airflow triggers, or operational dependencies for checkpoints and schema creation.</p>}
    <details open={filtersOpen} onToggle={(e) => setFiltersOpen(e.currentTarget.open)}><summary>Filters and evidence</summary>
    <div className="catalog-filters lineage-filters">
      <div className="field"><label htmlFor="lineage-depth">Depth</label><select id="lineage-depth" value={depth} onChange={(e) => setDepth(e.target.value)}>{[1, 2, 3, 4, 5, 8, 16, 32].map((n) => <option key={n}>{n}</option>)}</select></div>
      <div className="field"><label htmlFor="lineage-origin">Evidence</label><select id="lineage-origin" value={origin} onChange={(e) => setOrigin(e.target.value)}><option value="">All origins</option>{['dbt_manifest', 'airflow_dag', 'program_declared', 'program_runtime', 'trino_runtime', 'api_runtime'].map((v) => <option key={v} value={v}>{words(v)}</option>)}</select></div>
      <div className="field"><label htmlFor="lineage-window">Observed within</label><select id="lineage-window" value={windowDays} onChange={(e) => setWindowDays(e.target.value)}><option value="">Any time</option><option value="1">24 hours</option><option value="7">7 days</option><option value="30">30 days</option></select></div></div></details>
    <div className="object-toolbar lineage-toolbar" style={{ marginTop: 0 }}>
      <div className="toolbar-group">{direction !== "upstream" && <div className="segmented" role="group" aria-label="Lineage view"><button aria-pressed={mode === "explore"} onClick={() => setMode("explore")}>Explorer</button><button aria-pressed={mode === "impact"} onClick={() => { setMode("impact"); setFiltersOpen(true); setDirection("downstream"); }}>Impact analysis</button></div>}
        <label className="row"><input type="checkbox" checked={orchestration} onChange={(e) => setOrchestration(e.target.checked)} />Show orchestration</label>
        <label className="row"><input type="checkbox" checked={operational} onChange={(e) => setOperational(e.target.checked)} />Show operational dependencies</label>
        <label className="row"><input type="checkbox" checked={columns} onChange={(e) => setColumns(e.target.checked)} />Show columns</label>
        <label className="row" title={`Compact nodes switch on automatically above ${COMPACT_AFTER} objects`}><input type="checkbox" disabled={columns} checked={compact} onChange={(e) => setCompactPref(e.target.checked)} />Compact</label></div>
      <button className="btn secondary" onClick={() => { setOrchestration(false); setOperational(false); setColumns(false); setCompactPref(null); setOrigin(""); setWindowDays(""); setFind(""); setContainers(new Set()); setContents({}); setExpansions({}); setFocus(asset.id); setSelected(null); setConnection(null); setDirection("upstream"); setDepth("16"); setMode("explore"); setFiltersOpen(false); }}>Reset graph</button></div>
    {error && <p role="alert" className="error">{error}</p>}
    {view === "flow" && !!graph.edges.length && <nav className="lineage-stages" aria-label="Jump to lineage stage">
      {stages.map((stage, i) => <span key={stage.label}>{i > 0 && <span aria-hidden="true" className="muted">→</span>}
        <button className="btn sm secondary" title={`Show ${stage.label.toLowerCase()}`} onClick={() => jumpTo(stage.ids)}>{stage.label}<span className="badge">{stage.ids.length}</span></button></span>)}
      {orchestration && graph.nodes.some((a) => ["airflow", "airflow_dag"].includes(a.kind)) && <button className="btn sm secondary" onClick={() => jumpTo(graph.nodes.filter(a => ["airflow", "airflow_dag"].includes(a.kind)).map(a => a.id))}>Airflow orchestration</button>}
      <button className="text-button" onClick={() => instance.current?.fitView({ padding: .15, maxZoom: 1, duration: 300 })}>Fit entire chain</button>
    </nav>}
    <div className="lineage-summary"><span>{busy ? <span role="status">Loading lineage…</span> : <>Centered on <strong>{current?.name || asset.name}</strong> · {graph.nodes.length} objects · {graph.edges.length} connections</>}</span><span className="row" style={{ gap: 12 }}>{selected && <button className="text-button" onClick={() => setSelected(null)}>Clear path highlight</button>}{focus !== asset.id && <button className="text-button" onClick={() => setFocus(asset.id)}>Return to {asset.name}</button>}</span></div>
    {mode === "explore" ? <div className="lineage-explorer" role="region" aria-label="Interactive lineage graph">
      <ReactFlow<FlowNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onInit={(value) => { instance.current = value; }}
        onNodeMouseEnter={(_, node) => setHovered(node.id)} onNodeMouseLeave={() => setHovered(null)} onlyRenderVisibleElements={graph.nodes.length > 60}
        onNodeClick={(_, node) => choose(node.id)} onNodeDoubleClick={(_, node) => setFocus(node.id)} onEdgeClick={(_, edge) => { setConnection(graph.edges.find((e) => e.id === edge.id) || null); setSelected(null); }} onPaneClick={() => { setSelected(null); setConnection(null); }}
        nodesConnectable={false} deleteKeyCode={null} minZoom={.15} maxZoom={1.7} fitView fitViewOptions={{ maxZoom: 1, padding: .15 }} proOptions={{ hideAttribution: true }}>
        <Background color="var(--border)" gap={22} /><Controls showInteractive={false} /><MiniMap pannable zoomable nodeColor={(n) => n.id === focus ? "#0078d4" : "#66869e"} maskColor="var(--lineage-mask)" />
      </ReactFlow>
      <div className="lineage-find nodrag nopan"><label htmlFor="lineage-find">Find in graph</label><input id="lineage-find" type="search" value={find} onChange={(e) => setFind(e.target.value)} placeholder="Name or column" />{find && <div className="lineage-find-results">{raw.nodes.filter((a) => `${a.name} ${a.attributes.columns?.map((c) => c.name).join(" ") || ""}`.toLowerCase().includes(find.toLowerCase())).slice(0, 8).map((a) => <button key={a.id} onClick={() => { viewCenter.current = a.id; setContainers(previous => new Set([...previous, ...focusAncestors(raw, a.id)])); choose(a.id); instance.current?.fitView({ nodes: [{ id: a.id }], maxZoom: 1, duration: 300, padding: .5 }); setFind(""); }}>{a.name}</button>)}</div>}</div>
      {(selectedAsset || connection) && <aside className="lineage-inspector" aria-label="Lineage details"><button className="icon-btn lineage-close" aria-label="Close lineage details" title="Close" onClick={() => { setSelected(null); setConnection(null); }}><IconClose /></button>
        {selectedAsset && <><h2><ProductIcon kind={kind(selectedAsset)} />{selectedAsset.name}</h2><p className="muted">{objectKindLabel(selectedAsset.kind, selectedAsset.attributes.source_kind)} · {selectedAsset.path}</p><p>{selectedAsset.governance.description || selectedAsset.attributes.description || "No description yet."}</p>
          <dl className="kv"><dt>Domain</dt><dd>{selectedAsset.governance.domain || "Unassigned"}</dd><dt>Owner</dt><dd>{selectedAsset.owner_group_name || "Unassigned"}</dd><dt>Governance</dt><dd><span className={`badge catalog-status ${selectedAsset.governance.status || "draft"}`}>{selectedAsset.governance.status || "draft"}</span></dd><dt>SLA</dt><dd><span className={`badge catalog-status ${selectedAsset.service_level.status}`}>{words(selectedAsset.service_level.status)}</span></dd></dl>
          {!!selectedAsset.attributes.columns?.length && <details><summary>Columns ({selectedAsset.attributes.columns.length})</summary>{selectedAsset.attributes.columns.map((c) => <p key={c.name}><strong>{c.name}</strong> <span className="muted">{c.data_type}</span><br />{c.description}</p>)}</details>}
          <div className="row wrap"><button className="btn" onClick={() => open(selectedAsset)}>Open entity</button><button className="btn secondary" onClick={() => setFocus(selectedAsset.id)}>Center lineage</button></div></>}
        {connection && <><h2>Connection evidence</h2><p>{graph.nodes.find((a) => a.id === connection.source)?.name} → {graph.nodes.find((a) => a.id === connection.target)?.name}</p><dl className="kv"><dt>Relationship</dt><dd>{words(connection.relation)}</dd><dt>Purpose</dt><dd>{words(connection.category || "data")}</dd><dt>Origin</dt><dd>{(connection.evidence || [connection]).map((e) => words(e.origin)).filter((v, i, all) => all.indexOf(v) === i).join(", ")}</dd><dt>Revision / run</dt><dd className="mono">{connection.revision || "—"}</dd><dt>Observed</dt><dd>{new Date(connection.observed_at).toLocaleString()}</dd></dl>{!!connection.connections?.length && <><h3>Underlying connections</h3><ul>{connection.connections.map(item => <li key={item.id}>{item.source_name} → {item.target_name}<div className="muted">{words(item.relation)} · {words(item.origin)}</div></li>)}</ul></>}</>}
      </aside>}
    </div> : <div className="table-scroll"><table aria-label="Downstream impact"><thead><tr><th>Impacted object</th><th>Type</th><th>Hops</th><th>Owner</th><th>Governance / SLA</th></tr></thead><tbody>{impact.map((a) => <tr key={a.id}><td><button className="text-button object-name" onClick={() => open(a)}><ProductIcon kind={kind(a)} size={16} />{a.name}</button><div className="muted">{a.path}</div></td><td>{words(a.kind)}</td><td>{a.hops}</td><td>{a.owner_group_name || "Unassigned"}</td><td>{a.governance.status || "draft"} · {words(a.service_level.status)}</td></tr>)}{!impact.length && <tr><td colSpan={5}>No visible downstream objects within the selected depth and evidence filters.</td></tr>}</tbody></table><p className="footnote">Impact follows data dependencies; orchestration does not add data impact. Increase depth to include more reachable objects.</p></div>}
    <p className="footnote">Hover or select an object to trace its full upstream and downstream path; everything else dims. Read left to right: sources → transformations → inspected object. Drag to pan; scroll to zoom. Double-click to center, or expand either side. Dashed arrows show orchestration or operational dependencies; gray arrows indicate checkpoints or schema creation. Colors: dbt orange · Airflow green · programs blue · Trino purple. Columns show metadata; column-to-column transformations are not inferred.</p>
    {windowDays && <p className="footnote">The time filter shows currently stored relationships observed in this period; it is not a historical snapshot.</p>}
    {!busy && !graph.edges.length && <p className="notice">No visible lineage connections yet. Import native metadata or declare program inputs/outputs.</p>}
    {graph.truncated && <p className="notice warn" role="status">Graph limited to 150 nodes / 400 connections. Reduce depth or choose an evidence source.</p>}
    {!!graph.edges.length && <details className="card"><summary>Connection evidence ({graph.edges.length})</summary><table aria-label="Lineage connections"><thead><tr><th>From</th><th>Relationship</th><th>To</th><th>Evidence</th><th>Observed</th></tr></thead><tbody>{graph.edges.map((e) => <tr key={e.id}><td>{graph.nodes.find((a) => a.id === e.source)?.name}</td><td>{words(e.relation)}</td><td>{graph.nodes.find((a) => a.id === e.target)?.name}</td><td>{words(e.origin)}<div className="muted mono">{e.revision.slice(0, 12)}</div></td><td>{new Date(e.observed_at).toLocaleString()}</td></tr>)}</tbody></table></details>}
  </>;
}
