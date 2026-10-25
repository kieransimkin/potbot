import { xpraAppScope } from "./xpra-apps.ts";
import { appSessionId } from "../xpra-apps.ts";
import { boundedJSON, safeError } from "./security.ts";

const TOKEN = /^[A-Za-z0-9_-]{32,128}$/;
const PRIVATE = {"Cache-Control":"private, no-store"};

export async function browserLeaseProxy(request: Request, sessionId: string): Promise<Response> {
  const write = request.method !== "GET";
  const config = xpraAppScope(request, write);
  if (config instanceof Response) return config;
  if (!appSessionId(sessionId)) return safeError("Invalid browser session.", 400);
  let payload: Record<string, unknown> | null = null;
  if (write) {
    try {
      const decoded = await boundedJSON(request, 400);
      if (decoded === null || typeof decoded !== "object" || Array.isArray(decoded)) throw Error("Invalid body");
      payload = decoded as Record<string, unknown>;
      if (!(["acquire", "renew", "release"].includes(String(payload.action))) ||
          Object.keys(payload).some(k => !["action", "token"].includes(k)) ||
          (payload.action === "acquire" && "token" in payload) ||
          (payload.action !== "acquire" && (typeof payload.token !== "string" || !TOKEN.test(payload.token))))
        throw Error("Invalid lease operation");
    } catch { return safeError("Invalid browser reservation request.", 400); }
  }
  if (new URL(request.url).search) return safeError("Unexpected browser reservation query.", 400);
  try {
    const response = await fetch(`${config.base}/v1/xpra/apps/${sessionId}/browser/lease`, {
      method: request.method, redirect: "error", cache: "no-store",
      headers: {Authorization:`Bearer ${config.secret}`, ...(write ? {"Content-Type":"application/json"} : {})},
      ...(write ? {body: JSON.stringify(payload)} : {}), signal: AbortSignal.timeout(7000),
    });
    if (response.status === 409 || response.status === 404 || response.status === 503)
      return safeError(response.status === 409 ? "Browser reservation is already held or fenced." :
        "Browser reservation service unavailable.", response.status);
    if (!response.ok) return safeError("Browser reservation failed.", 502);
    if (Number(response.headers.get("content-length") || 0) > 4096) throw Error("Oversized lease response");
    const raw = await response.text();
    if (raw.length > 4096) throw Error("Oversized lease response");
    const data = JSON.parse(raw) as {workspaceId?: unknown;reservation?: unknown;token?: unknown;legacy?: unknown};
    if (data.workspaceId !== config.workspaceId || !data.reservation || typeof data.reservation !== "object" ||
        (data.token !== undefined && (typeof data.token !== "string" || !TOKEN.test(data.token))) ||
        (data.legacy !== undefined && data.legacy !== true))
      throw Error("Invalid lease result");
    return Response.json(data, {headers: PRIVATE});
  } catch { return safeError("Browser reservation service disconnected.", 503); }
}
