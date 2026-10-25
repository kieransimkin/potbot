/** Server-only strongly bounded communication with the durable chat API. */
import type { PotbotConfig } from "./potbot.ts";
import { upstream } from "./potbot.ts";
import { projectSessionList, projectSession, projectSummary } from "../chat-sessions.ts";
const UUID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
export function sessionId(input: string): boolean { return UUID.test(input); }

export async function readBoundedJSON(response: Response, limit = 180000): Promise<unknown> {
  if (Number(response.headers.get("content-length") || "0") > limit) throw new Error("Oversize chat response");
  const buffer = await response.arrayBuffer();
  if (buffer.byteLength > limit) throw new Error("Oversize chat response");
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer));
}
export async function loadSession(config: PotbotConfig, id: string) {
  if (!sessionId(id)) throw new Error("Invalid session ID");
  const result = await upstream(config, `/v1/ui/sessions/${id}`);
  if (!result.ok) throw new Error("Chat session unavailable");
  const projected = projectSession(await readBoundedJSON(result, 2400000));
  if (!projected) throw new Error("Invalid chat session response");
  return projected;
}
export async function sessionsList(config: PotbotConfig) {
  const result = await upstream(config, "/v1/ui/sessions");
  if (!result.ok) throw new Error("Chat sessions unavailable");
  const projected = projectSessionList(await readBoundedJSON(result, 16000));
  if (!projected) throw new Error("Invalid chat session list");
  return projected;
}
export async function createSession(config: PotbotConfig, id: string, title: string) {
  if (!sessionId(id) || !title.trim() || title.length > 120) throw new Error("Invalid session admission");
  const result = await upstream(config, "/v1/ui/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, title }),
  });
  if (!result.ok) throw new Error("Session admission unavailable");
  const value = await readBoundedJSON(result, 4096);
  const record = value && typeof value === "object" ? value as { created?: unknown } : null;
  const projected = projectSummary(value);
  if (!projected || projected.id !== id || typeof record?.created !== "boolean") throw new Error("Invalid session admission");
  return { ...projected, created: record.created };
}
export async function beginTurn(config: PotbotConfig, session: string, requestId: string, text: string) {
  if (!sessionId(session) || !sessionId(requestId) || !text.trim() || text.length > 16000)
    throw new Error("Invalid turn admission");
  const result = await upstream(config, `/v1/ui/sessions/${session}/turns`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ requestId, text }),
  });
  if (!result.ok) throw new Error("Turn admission unavailable");
  const value = await readBoundedJSON(result, 52000) as {
    id?: unknown; created?: unknown; state?: unknown; messages?: unknown;
  };
  if (!value || typeof value.id !== "string" || !sessionId(value.id) || value.created !== true ||
      value.state !== "pending" || !Array.isArray(value.messages) || value.messages.length > 24) throw new Error("Invalid or duplicate turn admission");
  let bytes = 0;
  const messages: { role: "user" | "assistant"; content: string }[] = [];
  for (const item of value.messages) {
    const m = item as { role?: unknown; content?: unknown };
    if (!m || (m.role !== "user" && m.role !== "assistant") ||
        typeof m.content !== "string" || m.content.length > 32000) throw new Error("Invalid stored chat context");
    bytes += new TextEncoder().encode(m.content).byteLength;
    messages.push({ role: m.role, content: m.content });
  }
  if (bytes > 48000 || messages.at(-1)?.role !== "user" || messages.at(-1)?.content !== text)
    throw new Error("Stored context does not match current turn");
  return { id: value.id, messages };
}
export async function finishTurn(config: PotbotConfig, session: string, turn: string,
  status: "completed" | "indeterminate", assistant: string | null, runId: string | null): Promise<boolean> {
  try {
    const result = await upstream(config, `/v1/ui/sessions/${session}/turns/${turn}/finish`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, assistant: status === "completed" ? assistant : null, runId }),
    }, 10000);
    return result.ok;
  } catch { return false; }
}
