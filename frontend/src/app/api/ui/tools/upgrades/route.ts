import { potbotConfigForRequest, upstream, upstreamFailure } from "../../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../../lib/server/security.ts";
import { upgradeListing } from "../../../../../lib/tool-upgrades.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (new URL(request.url).search) return safeError("Upgrade query not supported.", 400);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace backend unavailable.", 503);
  try {
    const result = await upstream(config, "/v1/ui/tools/upgrades", { method: "GET" });
    if (!result.ok) return upstreamFailure(result);
    const bytes = await result.arrayBuffer();
    if (bytes.byteLength > 65536) return safeError("Upgrade list exceeded size limit.", 502);
    const record = upgradeListing(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)), config.workspaceId);
    if (!record) return safeError("Invalid upgrade response.", 502);
    return Response.json({ ...record, workspaceId: config.workspaceId },
      { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Cannot load tool upgrades.", 503); }
}
