import { potbotConfigForRequest, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
const kinds = new Set(["sandbox", "agent", "operator", "tool", "skill", "worker", "workflow", "gateway", "model", "connector", "primitive"]);
const roles = new Set(["AGENTS.md", "MODEL.md", "MEMORY.md"]);
const name = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const maxRevision = Number.MAX_SAFE_INTEGER;

function target(value: Record<string, unknown>): boolean {
  return typeof value.kind === "string" && kinds.has(value.kind) &&
    typeof value.name === "string" && name.test(value.name) &&
    typeof value.role === "string" && roles.has(value.role);
}
function integer(value: unknown, min: number, max: number): boolean {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max;
}
async function boundedResponse(response: Response, bound: number): Promise<Response> {
  if (!response.ok) return response.status === 409 ? safeError("Context changed; reload the current revision.", 409) : upstreamFailure(response);
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > bound) return safeError("Context response exceeded its limit.", 502);
  const document: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  if (!document || typeof document !== "object" || Array.isArray(document)) return safeError("Invalid context response.", 502);
  return Response.json(document, { headers: { "Cache-Control": "private, no-store" } });
}

export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace backend unavailable.", 503);
  const params = new URL(request.url).searchParams;
  const keys = [...params.keys()];
  if (keys.length !== new Set(keys).size) return safeError("Duplicate context parameters.", 400);
  const action = params.get("action") ?? "document";
  if (!new Set(["document", "diff", "events"]).has(action)) return safeError("Invalid context action.", 400);
  const permitted = action === "events" ? ["action", "after", "limit"] :
    action === "diff" ? ["action", "kind", "name", "role", "before", "after"] :
    ["action", "kind", "name", "role", "limit"];
  if (keys.some(k => !permitted.includes(k))) return safeError("Unexpected context query.", 400);
  if (action !== "events" && !target(Object.fromEntries(params))) return safeError("Invalid context target.", 400);
  for (const key of ["before", "after", "limit"]) {
    if (!params.has(key)) continue;
    const value = params.get(key) ?? "";
    if (!/^\d{1,16}$/.test(value) || !integer(Number(value), key === "limit" || key === "before" ? 1 : 0,
        key === "limit" ? 200 : maxRevision)) return safeError("Invalid context revision or limit.", 400);
  }
  params.delete("action");
  const path = `/v1/ui/context/${action}${params.size ? `?${params.toString()}` : ""}`;
  try { return await boundedResponse(await upstream(config, path), action === "events" ? 250_000 : 500_000); }
  catch { return safeError("Context metadata unavailable.", 503); }
}

export async function POST(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Invalid context request origin.", 403);
  if (new URL(request.url).search) return safeError("Unexpected restore parameters.", 400);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace backend unavailable.", 503);
  let value: unknown;
  try { value = await boundedJSON(request, 8192); } catch { return safeError("Invalid context request.", 400); }
  if (!value || typeof value !== "object" || Array.isArray(value)) return safeError("Invalid context request.", 400);
  const document = value as Record<string, unknown>;
  const required = ["kind", "name", "role", "source_revision", "expected_revision", "idempotency_key", "confirm"];
  const keys = Object.keys(document);
  if (!required.every(k => keys.includes(k)) || keys.some(k => ![...required, "reason"].includes(k)) ||
      !target(document) || document.confirm !== true ||
      !integer(document.source_revision, 1, maxRevision) || !integer(document.expected_revision, 1, maxRevision) ||
      typeof document.idempotency_key !== "string" || document.idempotency_key.length < 1 || document.idempotency_key.length > 128 ||
      (document.reason !== undefined && (typeof document.reason !== "string" || document.reason.length > 900)))
    return safeError("Invalid or unconfirmed context restoration.", 400);
  try {
    return await boundedResponse(await upstream(config, "/v1/ui/context/restore", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(document),
    }), 8192);
  } catch { return safeError("Context restore unavailable.", 503); }
}
