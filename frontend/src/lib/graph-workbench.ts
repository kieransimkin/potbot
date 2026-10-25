import { boundedGraph } from "./graph.ts";
import type { GraphEdge, GraphNode, GraphProjection } from "./model.ts";

export type GraphView = {
  query: string;
  state: GraphNode["state"] | "all";
  kind: GraphNode["kind"] | "all";
  display: "3d" | "list";
  showNodes: boolean;
  showTimeline: boolean;
  showDiagnostics: boolean;
  selectedNodeId: string | null;
};

export const DEFAULT_GRAPH_VIEW: Readonly<GraphView> = Object.freeze({
  query: "", state: "all", kind: "all", display: "3d", showNodes: false,
  showTimeline: false, showDiagnostics: false, selectedNodeId: null,
});

const NODE_STATES = new Set(["all", "pending", "running", "completed", "rejected", "indeterminate"]);
const NODE_KINDS = new Set(["all", "objective", "hypothesis", "task", "evidence", "result"]);

export function sanitizedGraphView(input: unknown): GraphView {
  const data = input && typeof input === "object" && !Array.isArray(input)
    ? input as Record<string, unknown> : {};
  return {
    query: typeof data.query === "string" ? data.query.slice(0, 120) : "",
    state: typeof data.state === "string" && NODE_STATES.has(data.state)
      ? data.state as GraphView["state"] : "all",
    kind: typeof data.kind === "string" && NODE_KINDS.has(data.kind)
      ? data.kind as GraphView["kind"] : "all",
    display: data.display === "list" ? "list" : "3d",
    showNodes: data.showNodes === true,
    showTimeline: data.showTimeline === true,
    showDiagnostics: data.showDiagnostics === true,
    selectedNodeId: typeof data.selectedNodeId === "string" && data.selectedNodeId.length <= 128
      ? data.selectedNodeId : null,
  };
}

export type GraphSearchResult = {
  graph: GraphProjection;
  matches: number;
  contextNodes: number;
  totalNodes: number;
  totalEdges: number;
};

/** Filter recorded, public nodes and retain ancestry only for text-search context.
 * No hidden model text, unverifiable scores or speculative parent links are added.
 */
export function searchGraph(input: GraphProjection, filter: Pick<GraphView, "query" | "state" | "kind">): GraphSearchResult {
  const bounded = boundedGraph(input);
  const query = filter.query.trim().toLocaleLowerCase();
  const byId = new Map(bounded.nodes.map(node => [node.id, node]));
  const matchingIds = new Set<string>();
  for (const node of bounded.nodes) {
    if (filter.state !== "all" && node.state !== filter.state) continue;
    if (filter.kind !== "all" && node.kind !== filter.kind) continue;
    if (query && !node.label.toLocaleLowerCase().includes(query) && !node.id.toLocaleLowerCase().includes(query)) continue;
    matchingIds.add(node.id);
  }
  const visible = new Set(matchingIds);
  if (query) {
    // An ancestor supplies visual orientation, not another search match.
    for (const id of matchingIds) {
      let parent = byId.get(id)?.parentId;
      const visited = new Set<string>([id]);
      while (parent && byId.has(parent) && !visited.has(parent)) {
        visited.add(parent);
        visible.add(parent);
        parent = byId.get(parent)?.parentId;
      }
    }
  }
  const nodes = bounded.nodes.filter(node => visible.has(node.id));
  const edges: GraphEdge[] = bounded.edges.filter(edge => visible.has(edge.source) && visible.has(edge.target));
  return { graph: { revision: bounded.revision, nodes, edges }, matches: matchingIds.size,
    contextNodes: nodes.length - matchingIds.size, totalNodes: bounded.nodes.length,
    totalEdges: bounded.edges.length };
}

export type GraphEvent = { sequence: number; type: string };
export type Timeline = {
  runId: string;
  cursor: number;
  events: GraphEvent[];
  gapDetected: boolean;
  pages: number;
};

export const MAX_TIMELINE_EVENTS = 240;
const EVENT_TYPE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/;

/** The BFF gives only sequence/type, never untrusted raw node or execution data.
 * Missing source events remain marked as a gap; no synthetic timestamps are invented.
 */
export function mergeGraphTimeline(previous: Timeline | null, runId: string, raw: unknown,
  upstreamGap = false, maxEvents = MAX_TIMELINE_EVENTS): Timeline | null {
  if (!/^[a-f0-9]{32}$/i.test(runId) || !Array.isArray(raw) || raw.length > 200 ||
      !Number.isSafeInteger(maxEvents) || maxEvents < 1 || maxEvents > 2000) return null;
  const last = previous?.runId === runId ? previous : null;
  const incoming: GraphEvent[] = [];
  let prevSeq = 0;
  for (const item of raw) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const entry = item as Record<string, unknown>;
    if (!Number.isSafeInteger(entry.sequence) || (entry.sequence as number) <= prevSeq ||
        typeof entry.type !== "string" || !EVENT_TYPE.test(entry.type)) return null;
    const sequence = entry.sequence as number;
    prevSeq = sequence;
    incoming.push({ sequence, type: entry.type });
  }
  const gap = upstreamGap || (!!last && !!incoming.length && incoming[0].sequence > last.cursor + 1) ||
    (!last && !!incoming.length && incoming[0].sequence !== 1);
  const retained = !last || gap ? [] : last.events;
  const events = [...retained, ...incoming.filter(event => event.sequence > (gap ? 0 : last?.cursor ?? 0))]
    .slice(-maxEvents);
  return { runId, cursor: Math.max(last?.cursor ?? 0, incoming.at(-1)?.sequence ?? 0),
    events, gapDetected: (last?.gapDetected ?? false) || gap,
    pages: (last?.pages ?? 0) + 1 };
}

export function graphEventLabel(type: string): string {
  return type.replace(/[_.:-]/g, " ").replace(/\b[a-z]/g, c => c.toUpperCase());
}

export function graphCounts(nodes: GraphNode[]): Record<GraphNode["state"], number> {
  const values: Record<GraphNode["state"], number> = {
    pending: 0, running: 0, completed: 0, rejected: 0, indeterminate: 0,
  };
  for (const node of nodes) values[node.state] += 1;
  return values;
}
