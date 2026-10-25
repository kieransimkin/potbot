import { potbotConfigForRequest, upstream, upstreamFailure } from "../../../../../../../lib/server/potbot.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "../../../../../../../lib/server/security.ts";
import { upgradeRecord } from "../../../../../../../lib/tool-upgrades.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA = /^[a-f0-9]{64}$/;
export async function POST(request: Request, context: { params: Promise<{ id: string; action: string }> }): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Untrusted upgrade origin.", 403);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace backend unavailable.", 503);
  const { id, action } = await context.params;
  if (!UUID.test(id) || !["approve", "reject"].includes(action) || new URL(request.url).search)
    return safeError("Unapproved upgrade operation.", 400);
  const setupToken = process.env.POTBOT_UI_SETUP_TOKEN;
  if (!setupToken || !/^[!-~]{32,4096}$/.test(setupToken))
    return safeError("Operator setup permission is unavailable.", 503);
  let raw: unknown;
  try { raw = await boundedJSON(request, 2048); }
  catch { return safeError("Invalid upgrade decision.", 400); }
  const body = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : null;
  if (!body || Object.keys(body).sort().join(",") !== "confirm,evidenceSha256,revision" ||
      body.confirm !== true || !Number.isSafeInteger(body.revision) || Number(body.revision) < 0 ||
      typeof body.evidenceSha256 !== "string" || !SHA.test(body.evidenceSha256))
    return safeError("Confirmed revision and exact tested evidence required.", 400);
  try {
    const response = await upstream(config, `/v1/ui/tools/upgrades/${id}/${action}`, {
      method: "POST", headers: { "Origin": operator.publicOrigin,
        "X-Potbot-Setup-Token": setupToken, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) return response.status === 409 ? safeError("Upgrade revision has changed; reload.", 409) : upstreamFailure(response);
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength > 8192) return safeError("Upgrade response too large.", 502);
    const record = upgradeRecord(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)), config.workspaceId);
    if (!record || record.id !== id || record.evidenceSha256 !== body.evidenceSha256)
      return safeError("Unexpected upgrade confirmation.", 502);
    return Response.json(record, { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Cannot record upgrade decision.", 503); }
}
