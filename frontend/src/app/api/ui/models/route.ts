import { projectModels } from "../../../../lib/activity.ts";
import { potbotConfig, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Advisory catalogue, NOT an installed-model or download API. */
export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (new URL(request.url).search) return safeError("Unexpected model query.", 400);
  const config = potbotConfig();
  if (!config) return safeError("Potbot backend not configured.", 503);
  try {
    const upstreamResponse = await upstream(config, "/v1/models");
    if (!upstreamResponse.ok) return upstreamFailure(upstreamResponse);
    const bytes = await upstreamResponse.arrayBuffer();
    if (bytes.byteLength > 700_000) return safeError("Model catalogue too large.", 502);
    const catalog = projectModels(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)));
    if (!catalog) return safeError("Invalid model catalogue.", 502);
    return Response.json({ workspaceId: config.workspaceId, advisory: true,
      installTelemetry: false, models: catalog },
    { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Model catalogue unavailable.", 503); }
}
