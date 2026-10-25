import { potbotConfigForRequest } from "../../../../../lib/server/potbot.ts";
import { loadSession, sessionId } from "../../../../../lib/server/chat-sessions.ts";
import { requireOperator, safeError } from "../../../../../lib/server/security.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request, {params}: {params: Promise<{sessionId: string}>}): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Unknown workspace connection.", 404);
  const { sessionId: id } = await params;
  if (!sessionId(id)) return safeError("Invalid session identity.", 400);
  try {
    return Response.json({ workspaceId: config.workspaceId, ...await loadSession(config, id) },
      { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Chat session unavailable in this workspace.", 404); }
}
