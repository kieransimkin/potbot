/** Public, stable UI projection types. Backend never exports credentials or hidden reasoning. */
export type Workspace = {
  id: string;
  name: string;
  shortName: string;
  accent: string;
  summary: string;
  status: "idle" | "working" | "attention";
};

export type PanelId =
  | "chat" | "menu" | "graph" | "terminal" | "browser"
  | "preferences" | "downloads" | "installations" | "applications" | "activity";
export type UtilityPanel = Exclude<PanelId, "chat">;
export type DockEdge = "left" | "right" | "above" | "below";

export type ChatEntry = { id: string; role: "user" | "assistant" | "status"; text: string };
export type GraphNode = {
  id: string; parentId: string | null; label: string;
  state: "pending" | "running" | "completed" | "rejected" | "indeterminate";
  kind: "objective" | "hypothesis" | "task" | "evidence" | "result";
};
export type GraphEdge = { source: string; target: string; relation: string };
export type GraphProjection = { nodes: GraphNode[]; edges: GraphEdge[]; revision: number };

export type WorkspaceSession = { workspaceId: string; entries: ChatEntry[]; runId: string | null };
export type LoadState = "loading" | "ready" | "unavailable";

export const DEMO_WORKSPACES: Workspace[] = [
  { id: "demo-research", name: "Research", shortName: "R", accent: "#59d4ef", summary: "Hypotheses, evidence and investigation", status: "working" },
  { id: "demo-engineering", name: "Engineering", shortName: "E", accent: "#ae96f4", summary: "Coding and workflow verification", status: "idle" },
  { id: "demo-studio", name: "Studio", shortName: "S", accent: "#f5bd74", summary: "Creative research and KeywordMoves", status: "attention" },
  { id: "demo-monitor", name: "Monitoring", shortName: "M", accent: "#8de1ac", summary: "Model benchmarks and task health", status: "idle" },
];

/** Clearly artificial and never interpreted as persisted reasoning events. */
export const DEMO_GRAPH: GraphProjection = {
  revision: 1,
  nodes: [
    { id: "root", parentId: null, label: "Research the workflow", kind: "objective", state: "running" },
    { id: "a", parentId: "root", label: "Review available tools", kind: "task", state: "completed" },
    { id: "b", parentId: "root", label: "Find alternatives", kind: "hypothesis", state: "running" },
    { id: "c", parentId: "root", label: "Check constraints", kind: "hypothesis", state: "pending" },
    { id: "d", parentId: "a", label: "Evidence import", kind: "evidence", state: "completed" },
    { id: "e", parentId: "b", label: "Cost analysis", kind: "task", state: "pending" },
    { id: "f", parentId: "b", label: "Legacy approach", kind: "hypothesis", state: "rejected" },
    { id: "g", parentId: "c", label: "Validate results", kind: "result", state: "indeterminate" },
  ],
  edges: [
    { source: "root", target: "a", relation: "depends_on" },
    { source: "root", target: "b", relation: "proposes" },
    { source: "root", target: "c", relation: "proposes" },
    { source: "a", target: "d", relation: "supports" },
    { source: "b", target: "e", relation: "tests" },
    { source: "b", target: "f", relation: "refines" },
    { source: "c", target: "g", relation: "verifies" },
  ],
};
