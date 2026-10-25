import { potbotConfigForRequest, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../lib/server/security.ts";
import { projectOperatorStatus } from "../../../../lib/operator-status.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const auth = requireOperator(request);
  if (auth instanceof Response) return auth;
  if (new URL(request.url).search) return safeError("Unexpected operator status query.", 400);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace is not configured.", 503);
  try {
    const response = await upstream(config, "/v1/ui/operator", {}, 2500);
    if (!response.ok) return upstreamFailure(response);
    const buffer = await response.arrayBuffer();
    if (buffer.byteLength > 2048) return safeError("Invalid operator status.", 502);
    const raw: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer));
    const projection = projectOperatorStatus(raw, config.workspaceId);
    if (!projection) return safeError("Invalid operator status.", 502);
    return Response.json(projection, { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Operator status unavailable.", 503); }
}
