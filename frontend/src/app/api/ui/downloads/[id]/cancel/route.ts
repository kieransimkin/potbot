import { potbotConfig, upstream, upstreamFailure } from "../../../../../../lib/server/potbot.ts";
import { requireOperator, safeError, validOrigin } from "../../../../../../lib/server/security.ts";
export const runtime = "nodejs";
export async function POST(request: Request, context: { params: Promise<{ id: string }> }): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Untrusted request origin.", 403);
  const config = potbotConfig();
  if (!config) return safeError("Workspace backend not configured.", 503);
  const { id } = await context.params;
  if (!/^[0-9a-f]{8}-[0-9a-f-]{27,36}$/i.test(id)) return safeError("Invalid transfer ID.", 400);
  if (Number(request.headers.get("content-length") || "0") !== 0) return safeError("Cancellation body not allowed.", 400);
  try {
    const response = await upstream(config, `/v1/ui/downloads/${id}/cancel`, { method: "POST" });
    if (!response.ok) return upstreamFailure(response);
    return Response.json({ id, cancelRequested: true }, { status: 202, headers: { "Cache-Control": "no-store" } });
  } catch { return safeError("Transfer cancellation unavailable.", 503); }
}
