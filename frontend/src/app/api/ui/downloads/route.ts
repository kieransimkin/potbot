import { projectTransfers, validateArtifact } from "../../../../lib/ui-resources.ts";
import { potbotConfig, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "../../../../lib/server/security.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
async function jsonBounded(response: Response): Promise<unknown> {
  const body = await response.arrayBuffer();
  if (body.byteLength > 256000) throw new Error("Transfer response too large");
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
}
export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const config = potbotConfig();
  if (!config) return safeError("Workspace backend not configured.", 503);
  if (new URL(request.url).search) return safeError("Unexpected download query.", 400);
  try {
    const response = await upstream(config, "/v1/ui/downloads");
    if (!response.ok) return upstreamFailure(response);
    const document = await jsonBounded(response);
    if (!document || typeof document !== "object" || Array.isArray(document))
      return safeError("Invalid download response.", 502);
    const projected = projectTransfers({ ...document, workspaceId: config.workspaceId }, config.workspaceId);
    if (!projected) return safeError("Invalid transfer progress projection.", 502);
    return Response.json(projected, { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Workspace transfers unavailable.", 503); }
}
export async function POST(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Untrusted request origin.", 403);
  const config = potbotConfig();
  if (!config) return safeError("Workspace backend not configured.", 503);
  let raw: unknown;
  try { raw = await boundedJSON(request, 4096); } catch { return safeError("Invalid model artifact request.", 400); }
  const value = raw !== null && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : null;
  const artifact = value && validateArtifact(value.artifact);
  if (!value || Object.keys(value).sort().join(",") !== "artifact,confirm,requestId" ||
      value.confirm !== true || typeof value.requestId !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f-]{27,36}$/i.test(value.requestId) || !artifact)
    return safeError("Immutable artifact approval and request ID required.", 400);
  try {
    const response = await upstream(config, "/v1/ui/downloads", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ artifact, confirm: true, requestId: value.requestId }),
    });
    if (!response.ok) return upstreamFailure(response);
    const document = await jsonBounded(response);
    if (!document || typeof document !== "object" || Array.isArray(document))
      return safeError("Invalid transfer admission.", 502);
    const body = document;
    if (typeof body.id !== "string" || !/^[0-9a-f-]{36}$/i.test(body.id) ||
        !["queued", "downloading", "verifying", "installed", "failed", "cancelled", "indeterminate"].includes(String(body.state)))
      return safeError("Invalid model transfer admission.", 502);
    return Response.json({ id: body.id, state: body.state, created: body.created === true },
      { status: 202, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Transfer admission unavailable.", 503); }
}
