import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError } from "./security.ts";

const ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const SESSION_ID = /^[0-9a-f]{32}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const NANOSECONDS = /^\d{1,20}$/;
const NO_STORE = { "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" };

function authorized(request: Request) {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const config = potbotConfigForRequest(request);
  if (!config || request.headers.get("x-potbot-workspace") !== config.workspaceId)
    return safeError("Browser gallery is not available for this workspace.", 404);
  return config;
}

/** The BFF only proxies the current operator's fixed backend identity. */
export async function galleryList(request: Request): Promise<Response> {
  const config = authorized(request);
  if (config instanceof Response) return config;
  const params = new URL(request.url).searchParams;
  if ([...params.keys()].some(k => !["before", "limit"].includes(k)) ||
      [...params.keys()].length !== new Set(params.keys()).size) return safeError("Invalid gallery filters.", 400);
  const limit = params.get("limit") ?? "20";
  const before = params.get("before");
  if (!/^\d{1,2}$/.test(limit) || Number(limit) < 1 || Number(limit) > 50 ||
      (before !== null && (!/^\d{1,16}$/.test(before) || Number(before) < 1 || !Number.isSafeInteger(Number(before)))))
    return safeError("Invalid gallery cursor or page size.", 400);
  const query = new URLSearchParams({limit});
  if (before !== null) query.set("before", before);
  try {
    const response = await upstream(config, `/v1/browser/evidence?${query}`, {}, 8000);
    if (!response.ok) return upstreamFailure(response);
    const length = Number(response.headers.get("content-length") || 0);
    if (!Number.isFinite(length) || length > 65_536) throw Error("Gallery index too large");
    const buffer = await response.arrayBuffer();
    if (buffer.byteLength > 65_536) throw Error("Gallery index too large");
    const envelope = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(buffer)) as Record<string, unknown>;
    if (envelope.schema_version !== 1 || !envelope.data || typeof envelope.data !== "object") throw Error("Invalid gallery envelope");
    const data = envelope.data as Record<string, unknown>;
    if (data.schemaVersion !== 1 || !Array.isArray(data.items) || data.items.length > 50 ||
        !data.items.every(item => {
          if (!item || typeof item !== "object" || Array.isArray(item)) return false;
          const e = item as Record<string, unknown>;
          return typeof e.id === "string" && ID.test(e.id) &&
            typeof e.sessionId === "string" && SESSION_ID.test(e.sessionId) &&
            typeof e.kind === "string" && ["observation", "action", "capture"].includes(e.kind) &&
            typeof e.capturedUnixNs === "string" && NANOSECONDS.test(e.capturedUnixNs) &&
            typeof e.manifestSha256 === "string" && SHA256.test(e.manifestSha256) &&
            typeof e.observationSha256 === "string" && SHA256.test(e.observationSha256) &&
            typeof e.hasImage === "boolean";
        }) || (data.nextCursor !== null &&
        (!Number.isSafeInteger(data.nextCursor) || (data.nextCursor as number) < 1)))
      throw Error("Invalid gallery index");
    return Response.json(data, {headers: NO_STORE});
  } catch { return safeError("Browser gallery is unavailable.", 503); }
}

export async function galleryPart(request: Request, id: string, part: string): Promise<Response> {
  const config = authorized(request);
  if (config instanceof Response) return config;
  if (!ID.test(id) || !["image", "observation"].includes(part) || new URL(request.url).search)
    return safeError("Invalid browser evidence reference.", 400);
  try {
    const response = await upstream(config, `/v1/browser/evidence/${id}/${part}`, {}, 9000);
    if (!response.ok) return upstreamFailure(response);
    const maximum = part === "image" ? 600_000 : 250_000;
    if (Number(response.headers.get("content-length") || 0) > maximum) throw Error("Evidence exceeds bounds");
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.length > maximum || bytes.length === 0) throw Error("Evidence exceeds bounds");
    const expectedMime = part === "image" ? "image/jpeg" : "application/json";
    if (!response.headers.get("content-type")?.startsWith(expectedMime)) throw Error("Unexpected evidence MIME");
    if (part === "image" && !(bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 &&
          bytes.at(-2) === 255 && bytes.at(-1) === 217)) throw Error("Invalid image framing");
    return new Response(bytes, { headers: {...NO_STORE, "Content-Type": expectedMime,
      "Content-Security-Policy": "default-src 'none'"} });
  } catch { return safeError("Browser evidence could not be verified.", 503); }
}
