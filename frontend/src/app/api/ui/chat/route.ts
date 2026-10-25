import { validChatInput } from "../../../../lib/live-schema.ts";
import { potbotConfig, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "../../../../lib/server/security.ts";

export const runtime = "nodejs";
export async function POST(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Untrusted request origin.", 403);
  const config = potbotConfig();
  if (!config) return safeError("Potbot backend is unavailable.", 503);
  if (Number(request.headers.get("content-length") || "0") > 64_000) return safeError("Conversation is too large.", 413);
  let raw: unknown;
  try { raw = await boundedJSON(request, 64_000); } catch { return safeError("Invalid chat request.", 400); }
  const input = validChatInput(raw, config.workspaceId);
  if (!input) return safeError("Invalid workspace or conversation payload.", 400);
  try {
    const response = await upstream(config, "/v1/chat/completions", {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ model: config.model, messages: input.messages, stream: true }),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(300000)]),
    });
    if (!response.ok) return upstreamFailure(response);
    if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream"))
      return safeError("Backend did not return an inference stream.", 502);
    const runId = response.headers.get("X-Potbot-Run-Id");
    return new Response(response.body, { status: 200, headers: {
      "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store, no-transform",
      "X-Accel-Buffering": "no", ...(runId && /^[0-9a-f]{32}$/i.test(runId) ? { "X-Potbot-Run-Id": runId } : {}),
    } });
  } catch { return safeError("Potbot inference transport failed or timed out.", 502); }
}
