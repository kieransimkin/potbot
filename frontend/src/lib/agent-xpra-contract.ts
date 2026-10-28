/** S41: non-secret read-only projections of independently scoped Xpra streams. */
import { validXpraWindow, XWINDOW_ID, WORKSPACE_ID } from "./xpra-contract.ts";
import type { XWindowInfo } from "./xpra-contract.ts";

const SESSION = /^[0-9a-f]{32}$/;
const GLOBAL = /^agentx11:([0-9a-f-]{36}):([0-9a-f]{32}):(xwin-[0-9a-f]{1,8})$/;

export type AgentXpraSession = {
  id: string; agentId: string; title: string; state: "running";
  startedAt: number; viewOnly: true;
};
export type AgentWindowInfo = XWindowInfo & {
  key: string; workspaceId: string; sessionId: string; agentId: string;
  connected: boolean; epoch: number;
};

export function agentXpraSessions(value: unknown, workspaceId: string): AgentXpraSession[] | null {
  if (!value || typeof value !== "object" || Array.isArray(value) || !WORKSPACE_ID.test(workspaceId)) return null;
  const document = value as Record<string, unknown>;
  if (document.workspaceId !== workspaceId || document.viewOnly !== true ||
      !Array.isArray(document.sessions) || document.sessions.length > 12) return null;
  const seen = new Set<string>();
  const result: AgentXpraSession[] = [];
  for (const item of document.sessions) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const row = item as Record<string, unknown>;
    if (typeof row.id !== "string" || !SESSION.test(row.id) ||
        typeof row.agentId !== "string" || !WORKSPACE_ID.test(row.agentId) ||
        row.agentId !== row.agentId.toLowerCase() || row.state !== "running" ||
        typeof row.title !== "string" || !row.title || row.title.length > 80 ||
        !Number.isSafeInteger(row.startedAt) || Number(row.startedAt) < 0 ||
        row.viewOnly !== true || seen.has(row.id)) return null;
    seen.add(row.id);
    result.push({id: row.id, agentId: row.agentId, title: row.title,
      state: "running", startedAt: Number(row.startedAt), viewOnly: true});
  }
  return result;
}

export function agentWindowKey(workspaceId: string, sessionId: string, winId: string): string {
  if (!WORKSPACE_ID.test(workspaceId) || workspaceId !== workspaceId.toLowerCase() ||
      !SESSION.test(sessionId) || !XWINDOW_ID.test(winId)) throw Error("Invalid agent Xpra window scope");
  return `agentx11:${workspaceId}:${sessionId}:${winId}`;
}

export function parseAgentWindowKey(key: string): {workspaceId: string; sessionId: string; winId: string} | null {
  const match = GLOBAL.exec(key);
  if (!match || !WORKSPACE_ID.test(match[1])) return null;
  try {
    if (agentWindowKey(match[1], match[2], match[3]) !== key) return null;
  } catch { return null; }
  return {workspaceId: match[1], sessionId: match[2], winId: match[3]};
}

export function scopedAgentWindow(workspaceId: string, session: AgentXpraSession,
                                  raw: unknown, epoch: number): AgentWindowInfo | null {
  const window = validXpraWindow(raw);
  if (!window || !Number.isSafeInteger(epoch) || epoch < 1 || epoch > 0x7fffffff) return null;
  const key = agentWindowKey(workspaceId, session.id, window.id);
  return {...window, key, workspaceId, sessionId: session.id, agentId: session.agentId,
    connected: true, epoch};
}
