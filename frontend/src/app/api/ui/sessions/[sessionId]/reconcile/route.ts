import { potbotConfigForRequest, upstream } from "../../../../../../lib/server/potbot.ts";
import { sessionId } from "../../../../../../lib/server/chat-sessions.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "../../../../../../lib/server/security.ts";
export const runtime = "nodejs";
export async function POST(request: Request, { params }: { params: Promise<{sessionId: string}>}): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Untrusted reconciliation origin.", 403);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Unknown workspace connection.", 404);
  const { sessionId: id } = await params;
  if (!sessionId(id)) return safeError("Invalid session ID.", 400);
  let raw: unknown;
  try { raw = await boundedJSON(request, 1024); } catch { return safeError("Invalid reconciliation request.", 400); }
  const body = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : null;
  if (!body || Object.keys(body).sort().join(",") !== "confirm,turnId,workspaceId" ||
      body.confirm !== true || body.workspaceId !== config.workspaceId ||
      typeof body.turnId !== "string" || !sessionId(body.turnId))
    return safeError("Reconciliation requires an explicit pending turn acknowledgement.", 400);
  try {
    const response = await upstream(config, `/v1/ui/sessions/${id}/turns/${body.turnId}/reconcile`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ acknowledgeUncertain: true }),
    });
    if (!response.ok) return safeError("Turn is still running or cannot be reconciled.", 409);
    return Response.json({ id: body.turnId, state: "indeterminate", replayed: false },
      { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Reconciliation service unavailable.", 503); }
}
