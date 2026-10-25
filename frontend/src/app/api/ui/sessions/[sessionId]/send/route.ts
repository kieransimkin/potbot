import { potbotConfigForRequest, upstream, upstreamFailure } from "../../../../../../lib/server/potbot.ts";
import { beginTurn, finishTurn, sessionId } from "../../../../../../lib/server/chat-sessions.ts";
import { persistentChatStream } from "../../../../../../lib/server/chat-stream-persist.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "../../../../../../lib/server/security.ts";

export const runtime = "nodejs";
export async function POST(request: Request, { params }: { params: Promise<{ sessionId: string }> }): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Untrusted inference request origin.", 403);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Unknown workspace connection.", 404);
  const { sessionId: chatId } = await params;
  if (!sessionId(chatId)) return safeError("Invalid session identity.", 400);
  let value: unknown;
  try { value = await boundedJSON(request, 18000); } catch { return safeError("Invalid durable chat request.", 400); }
  const body = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
  if (!body || Object.keys(body).sort().join(",") !== "requestId,text,workspaceId" ||
      body.workspaceId !== config.workspaceId || typeof body.requestId !== "string" ||
      typeof body.text !== "string" || !sessionId(body.requestId) || body.text.length > 16000 || !body.text.trim())
    return safeError("Invalid scoped chat request.", 400);
  let turn;
  try { turn = await beginTurn(config, chatId, body.requestId, body.text); }
  catch { return safeError("Turn is unavailable, duplicated or blocked by a previous request.", 409); }
  let runId: string | null = null;
  try {
    const response = await upstream(config, "/v1/chat/completions", {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ model: config.model, messages: turn.messages, stream: true }),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(300000)]),
    }, 300000);
    if (!response.ok || !response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
      await finishTurn(config, chatId, turn.id, "indeterminate", null, null);
      return !response.ok ? upstreamFailure(response) : safeError("Backend inference stream missing.", 502);
    }
    const candidate = response.headers.get("X-Potbot-Run-Id");
    if (candidate && /^[0-9a-f]{32}$/.test(candidate)) runId = candidate;
    const stream = persistentChatStream(response.body, config, chatId, turn.id, runId);
    return new Response(stream, { headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no",
      "X-Potbot-Session-Id": chatId,
      ...(runId ? { "X-Potbot-Run-Id": runId } : {}),
    } });
  } catch {
    await finishTurn(config, chatId, turn.id, "indeterminate", null, runId);
    return safeError("Model request could not complete; its outcome is uncertain.", 502);
  }
}
