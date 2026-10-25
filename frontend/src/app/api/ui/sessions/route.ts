import { potbotConfigForRequest } from "../../../../lib/server/potbot.ts";
import { createSession, sessionsList } from "../../../../lib/server/chat-sessions.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Unknown workspace connection.", 404);
  try {
    return Response.json({ workspaceId: config.workspaceId, items: await sessionsList(config) },
      { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Chat session history unavailable.", 503); }
}
export async function POST(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Untrusted session request.", 403);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Unknown workspace connection.", 404);
  let body: unknown;
  try { body = await boundedJSON(request, 4096); } catch { return safeError("Invalid session request.", 400); }
  const value = body && typeof body === "object" && !Array.isArray(body) ? body as Record<string, unknown> : null;
  if (!value || Object.keys(value).sort().join(",") !== "id,title,workspaceId" ||
      value.workspaceId !== config.workspaceId || typeof value.id !== "string" || typeof value.title !== "string")
    return safeError("Invalid workspace or session request.", 400);
  try {
    return Response.json(await createSession(config, value.id, value.title),
      { status: 201, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Could not create the chat session.", 409); }
}
