import { projectPreferences, validateSettings } from "../../../../lib/ui-resources.ts";
import { potbotConfig, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "../../../../lib/server/security.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
async function jsonBounded(response: Response): Promise<unknown> {
  const body = await response.arrayBuffer();
  if (body.byteLength > 8192) throw new Error("Preferences response too large");
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
}
export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const config = potbotConfig();
  if (!config) return safeError("Workspace backend not configured.", 503);
  if (new URL(request.url).search) return safeError("Unexpected preferences query.", 400);
  try {
    const response = await upstream(config, "/v1/ui/preferences");
    if (!response.ok) return upstreamFailure(response);
    const document = await jsonBounded(response);
    if (!document || typeof document !== "object" || Array.isArray(document))
      return safeError("Invalid preferences response.", 502);
    const value = projectPreferences({ ...document, workspaceId: config.workspaceId }, config.workspaceId);
    if (!value) return safeError("Invalid preferences projection.", 502);
    return Response.json(value, { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Workspace preferences unavailable.", 503); }
}
export async function PUT(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Untrusted request origin.", 403);
  const config = potbotConfig();
  if (!config) return safeError("Workspace backend not configured.", 503);
  let raw: unknown;
  try { raw = await boundedJSON(request, 4096); } catch { return safeError("Invalid preferences payload.", 400); }
  const value = raw !== null && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : null;
  const settings = value && validateSettings(value.settings);
  if (!value || Object.keys(value).sort().join(",") !== "revision,settings" ||
      !Number.isSafeInteger(value.revision) || (value.revision as number) < 0 || !settings)
    return safeError("Invalid preference revision or settings.", 400);
  try {
    const response = await upstream(config, "/v1/ui/preferences", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision: value.revision, settings }),
    });
    if (!response.ok) return response.status === 409 ? safeError("Preferences changed in another session; reload.", 409) : upstreamFailure(response);
    const document = await jsonBounded(response);
    if (!document || typeof document !== "object" || Array.isArray(document))
      return safeError("Invalid preference write response.", 502);
    const projected = projectPreferences({ ...document, workspaceId: config.workspaceId }, config.workspaceId);
    if (!projected) return safeError("Invalid preference write response.", 502);
    return Response.json(projected, { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Unable to save workspace preferences.", 503); }
}
