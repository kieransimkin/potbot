/** Whitelisted projections: never relay raw reasoning snapshots or claim tokens. */
import type { GraphNode, GraphProjection, Workspace } from "./model";

const STATES = new Set(["pending", "running", "completed", "rejected", "pruned", "indeterminate"]);
const MAX_TEXT = 180;
export const RUN_ID = /^[0-9a-f]{32}$/i;
const asRecord = (v: unknown): Record<string, unknown> | null =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : null;

export function parseWorkspaceList(input: unknown): Workspace[] {
  const record = asRecord(input);
  const raw = Array.isArray(record?.workspaces) ? record.workspaces : [];
  const ids = new Set<string>();
  return raw.slice(0, 50).flatMap(item => {
    const data = asRecord(item);
    if (!data || typeof data.id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(data.id) || ids.has(data.id)) return [];
    ids.add(data.id);
    const name = typeof data.name === "string" && data.name.trim() ? data.name.trim().slice(0, 80) : "Workspace";
    const status = data.status === "working" || data.status === "attention" ? data.status : "idle";
    return [{ id: data.id, name, shortName: name.charAt(0).toUpperCase(),
      accent: typeof data.accent === "string" && /^#[0-9a-f]{6}$/i.test(data.accent) ? data.accent : "#59d4ef",
      summary: typeof data.summary === "string" ? data.summary.slice(0, 160) : "Potbot workspace",
      status } satisfies Workspace];
  });
}

/** The current backend exposes a reasoning *tree snapshot*, not a stable graph projection. */
export function projectReasoningState(raw: unknown): GraphProjection | null {
  const record = asRecord(raw);
  const tree = asRecord(record?.snapshot);
  if (!tree || !Array.isArray(tree.nodes)) return null;
  const nodes: GraphNode[] = [];
  const ids = new Set<string>();
  for (const rawNode of tree.nodes.slice(0, 1500)) {
    const item = asRecord(rawNode);
    if (!item || typeof item.id !== "string" || item.id.length > 128 || ids.has(item.id)) continue;
    ids.add(item.id);
    const parentId = typeof item.parent_id === "string" && item.parent_id.length <= 128 ? item.parent_id : null;
    const label = typeof item.hypothesis === "string" ? item.hypothesis.slice(0, MAX_TEXT) : "Reasoning step";
    const state = typeof item.state === "string" && STATES.has(item.state) ? item.state : "indeterminate";
    nodes.push({ id: item.id, parentId, label, state: state === "pruned" ? "rejected" : state as GraphNode["state"],
      kind: item.role === "investigator" ? "evidence" : item.role === "synthesizer" ? "result" : "hypothesis" });
  }
  const edges = nodes.filter(n => n.parentId && ids.has(n.parentId)).slice(0, 4500).map(n => ({ source: n.parentId!, target: n.id, relation: "branch" }));
  const revision = Number.isSafeInteger(tree.revision) && (tree.revision as number) >= 0 ? tree.revision as number : 0;
  return { revision, nodes, edges };
}

export type EventPage = { cursor: number; events: { sequence: number; type: string }[]; gap: boolean; more: boolean };
export function summarizeEventPage(raw: unknown, after: number): EventPage | null {
  const record = asRecord(raw);
  if (!record || record.object !== "list" || !Array.isArray(record.data)) return null;
  const events: EventPage["events"] = [];
  let cursor = after, gap = false;
  for (const item of record.data.slice(0, 200)) {
    const value = asRecord(item);
    if (!value || !Number.isSafeInteger(value.sequence) || (value.sequence as number) <= 0 ||
      typeof value.type !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/.test(value.type)) return null;
    const sequence = value.sequence as number;
    if (sequence !== cursor + 1) gap = true;
    if (sequence <= cursor) return null;
    cursor = sequence;
    events.push({ sequence, type: value.type.slice(0, 80) });
  }
  return { cursor, events, gap, more: record.data.length >= 200 };
}

export function validChatInput(value: unknown, allowedWorkspace: string): { messages: { role: "user" | "assistant"; content: string }[] } | null {
  const data = asRecord(value);
  if (!data || data.workspaceId !== allowedWorkspace || !Array.isArray(data.messages) ||
      data.messages.length < 1 || data.messages.length > 24) return null;
  const messages: { role: "user" | "assistant"; content: string }[] = [];
  let bytes = 0;
  for (const m of data.messages) {
    const msg = asRecord(m);
    if (!msg || (msg.role !== "user" && msg.role !== "assistant") ||
        typeof msg.content !== "string" || !msg.content.trim() || msg.content.length > 16000) return null;
    bytes += new TextEncoder().encode(msg.content).length;
    messages.push({ role: msg.role, content: msg.content });
  }
  if (messages[messages.length - 1].role !== "user" || bytes > 48000) return null;
  return { messages };
}
