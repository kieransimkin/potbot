/** Strict browser projections for scoped, durable chat sessions. */
import type { ChatEntry } from "./model";

const UUID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const RUN = /^[0-9a-f]{32}$/;
const record = (x: unknown): Record<string, unknown> | null =>
  x !== null && typeof x === "object" && !Array.isArray(x) ? x as Record<string, unknown> : null;
export type ChatSessionSummary = { id: string; title: string; revision: number; updatedAt: string };
export type ChatTurn = {
  id: string; ordinal: number; user: string; assistant: string | null;
  state: "pending" | "completed" | "indeterminate"; runId: string | null;
};
export type ChatSessionDetails = { session: ChatSessionSummary; turns: ChatTurn[] };

export function projectSummary(raw: unknown): ChatSessionSummary | null {
  const v = record(raw);
  if (!v || typeof v.id !== "string" || !UUID.test(v.id) || typeof v.title !== "string" ||
      !v.title.trim() || v.title.length > 120 || typeof v.updatedAt !== "string" ||
      v.updatedAt.length > 55 || !v.updatedAt.includes("T") ||
      !Number.isSafeInteger(v.revision) || (v.revision as number) < 0 || (v.revision as number) > 200)
    return null;
  return { id: v.id, title: v.title, revision: v.revision as number, updatedAt: v.updatedAt };
}
export function projectSessionList(raw: unknown): ChatSessionSummary[] | null {
  const v = record(raw);
  if (!v || !Array.isArray(v.items) || v.items.length > 50) return null;
  const items = v.items.map(projectSummary);
  if (items.some(v => v === null) || new Set(items.map(v => v?.id)).size !== items.length) return null;
  return items as ChatSessionSummary[];
}
export function projectSession(raw: unknown): ChatSessionDetails | null {
  const value = record(raw), summary = projectSummary(value?.session);
  if (!value || !summary || !Array.isArray(value.turns) || value.turns.length > 200) return null;
  const turns: ChatTurn[] = [];
  for (const rawTurn of value.turns) {
    const v = record(rawTurn);
    if (!v || typeof v.id !== "string" || !UUID.test(v.id) ||
        !Number.isSafeInteger(v.ordinal) || (v.ordinal as number) < 1 ||
        typeof v.user !== "string" || !v.user.trim() || v.user.length > 16000 ||
        !["pending", "completed", "indeterminate"].includes(String(v.state)) ||
        !(v.assistant === null || (typeof v.assistant === "string" && v.assistant.length <= 32000)) ||
        (v.state === "completed") !== (typeof v.assistant === "string") ||
        !(v.runId === null || (typeof v.runId === "string" && RUN.test(v.runId))) ||
        (turns.length && v.ordinal !== turns[turns.length-1].ordinal + 1)) return null;
    turns.push({ id: v.id, ordinal: v.ordinal as number, user: v.user,
      assistant: v.assistant as string | null, state: v.state as ChatTurn["state"],
      runId: v.runId as string | null });
  }
  return { session: summary, turns };
}
export function conversationEntries(details: ChatSessionDetails): ChatEntry[] {
  const result: ChatEntry[] = [];
  for (const turn of details.turns) {
    result.push({ id: `${turn.id}-user`, role: "user", text: turn.user });
    if (turn.state === "completed") result.push({ id: `${turn.id}-assistant`, role: "assistant", text: turn.assistant ?? "" });
    else result.push({ id: `${turn.id}-state`, role: "status", text:
      turn.state === "pending" ? "Previous request is pending; refresh before sending again." :
        "Previous inference outcome is indeterminate. It has not been retried." });
  }
  return result;
}
