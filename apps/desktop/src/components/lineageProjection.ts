import type { CatalogGraph } from "../api";

type Entity = CatalogGraph["nodes"][number];
type Link = CatalogGraph["edges"][number];

/** Ancestors needed to keep a specifically inspected child visible. */
export function focusAncestors(graph: CatalogGraph, focus: string): Set<string> {
  const nodes = new Map(graph.nodes.map(node => [node.id, node]));
  const ancestors = new Set<string>();
  let current = nodes.get(focus)?.lineage_parent_id;
  while (current && nodes.has(current) && !ancestors.has(current) && current !== focus) {
    ancestors.add(current);
    current = nodes.get(current)?.lineage_parent_id;
  }
  return ancestors;
}

/** Include every visible descendant of a nested container for path highlighting. */
export function containerMembers(graph: CatalogGraph, id: string): string[] {
  const members = new Set([id]);
  for (let changed = true; changed;) {
    changed = false;
    for (const node of graph.nodes) if (node.lineage_parent_id && members.has(node.lineage_parent_id) && !members.has(node.id)) {
      members.add(node.id); changed = true;
    }
  }
  return [...members];
}

/** A visual projection only: never infer data flow from container membership. */
export function projectLineage(graph: CatalogGraph, expanded: Set<string>): CatalogGraph {
  const nodes = new Map(graph.nodes.map(node => [node.id, node]));
  const children = new Map<string, number>();
  for (const node of graph.nodes) if (node.lineage_parent_id && nodes.has(node.lineage_parent_id)) {
    children.set(node.lineage_parent_id, (children.get(node.lineage_parent_id) || 0) + 1);
  }
  function representative(id: string): string {
    let result = id, current = nodes.get(id)?.lineage_parent_id;
    const seen = new Set([id]);
    while (current && nodes.has(current) && !seen.has(current)) {
      seen.add(current);
      if (!expanded.has(current)) result = current;
      current = nodes.get(current)?.lineage_parent_id;
    }
    return result;
  }
  const visible = graph.nodes.filter(node => representative(node.id) === node.id).map(node => ({
    ...node,
    ...(children.has(node.id) ? { lineage_child_count: children.get(node.id) } : {}),
    ...(!node.lineage_parent_id || representative(node.lineage_parent_id) !== node.lineage_parent_id
      ? { lineage_parent_id: undefined } : {}),
  }));
  const groups = new Map<string, Link[]>();
  for (const edge of graph.edges) {
    if (edge.relation === "contains") continue;
    const source = representative(edge.source), target = representative(edge.target);
    if (source === target) continue;
    const key = JSON.stringify([source, target, edge.relation, edge.category || "data", !!edge.control]);
    groups.set(key, [...(groups.get(key) || []), edge]);
  }
  const edges: Link[] = [];
  for (const [key, originals] of groups) {
    const [source, target] = JSON.parse(key) as string[];
    const primary = originals[0];
    const combined = originals.length > 1 || source !== primary.source || target !== primary.target;
    const evidence = new Map(originals.flatMap(edge => edge.evidence || [edge]).map(item => [item.id, item]));
    edges.push({ ...primary, source, target,
      id: combined ? `summary:${source}:${target}:${primary.relation}:${primary.category || "data"}:${!!primary.control}` : primary.id,
      collapsed_count: originals.length,
      evidence: [...evidence.values()],
      connections: originals.map(edge => ({ id: edge.id, source: edge.source, target: edge.target,
        source_name: nodes.get(edge.source)?.name || edge.source, target_name: nodes.get(edge.target)?.name || edge.target,
        relation: edge.relation, origin: edge.origin, revision: edge.revision, observed_at: edge.observed_at })),
    });
  }
  for (const node of visible) if (children.has(node.id)) {
    node.upstream_count = new Set(edges.filter(edge => edge.target === node.id).map(edge => edge.source)).size;
    node.downstream_count = new Set(edges.filter(edge => edge.source === node.id).map(edge => edge.target)).size;
  }
  return { nodes: visible as Entity[], edges, truncated: graph.truncated };
}
