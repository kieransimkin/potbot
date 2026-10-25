import type { GraphNode, GraphProjection } from "./model";

export function boundedGraph(input: GraphProjection, maxNodes = 1500, maxEdges = 4500): GraphProjection {
  const accepted: GraphNode[] = [];
  const found = new Set<string>();
  for (const item of input.nodes) {
    if (!item.id || found.has(item.id)) continue;
    accepted.push(item);
    found.add(item.id);
    if (accepted.length >= maxNodes) break;
  }
  return { revision: input.revision, nodes: accepted, edges: input.edges.filter(e => found.has(e.source) && found.has(e.target)).slice(0, maxEdges) };
}

/** Deterministic radial DAG placement: no per-frame CPU force simulation. */
export function graphPositions(data: GraphProjection): Map<string, [number, number, number]> {
  const list = boundedGraph(data);
  const depth = new Map<string, number>();
  const parent = new Map(list.nodes.map(n => [n.id, n.parentId]));
  function getDepth(id: string, seen = new Set<string>()): number {
    if (depth.has(id)) return depth.get(id)!;
    if (seen.has(id)) return 0;
    seen.add(id);
    const p = parent.get(id);
    const d = p && parent.has(p) ? Math.min(12, 1 + getDepth(p, seen)) : 0;
    depth.set(id, d);
    return d;
  }
  for (const item of list.nodes) getDepth(item.id);
  const layers = new Map<number, string[]>();
  for (const node of list.nodes) {
    const d = depth.get(node.id)!;
    layers.set(d, [...(layers.get(d) ?? []), node.id]);
  }
  const locations = new Map<string, [number, number, number]>();
  for (const [layer, ids] of layers) ids.forEach((id, i) => {
    const theta = 2 * Math.PI * (i / Math.max(ids.length, 1)) + layer * .75;
    const radius = layer === 0 ? 0 : 35 + layer * 25 + Math.sqrt(ids.length) * 4;
    locations.set(id, [Math.cos(theta) * radius, (layer * -40) + Math.sin(theta * 2) * 9, Math.sin(theta) * radius]);
  });
  return locations;
}

export function graphStateColour(state: GraphNode["state"]): number {
  return ({ pending: 0xc0a05b, running: 0x58b6ea, completed: 0x58c4a5, rejected: 0x866e80, indeterminate: 0xd67f80 } as const)[state];
}
