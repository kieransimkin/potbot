import { xpraBridgeConfigForWorkspace } from "./xpra.ts";
import { projectAppInventory, projectAppSession, validAppKind, appSessionId } from "../xpra-apps.ts";
import { requireOperator, safeError, validOrigin, boundedJSON } from "./security.ts";

export function xpraAppScope(request: Request, write = false) {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (write && !validOrigin(request, operator)) return safeError("Untrusted application request.", 403);
  const selected = request.headers.get("x-potbot-workspace");
  const config = xpraBridgeConfigForWorkspace(selected);
  if (!config || selected !== config.workspaceId)
    return safeError("No application service for this workspace.", 404);
  if (new URL(request.url).search) return safeError("Unexpected application query.", 400);
  return config;
}

async function boundedResponse(response: Response, maximum: number): Promise<unknown> {
  const length = Number(response.headers.get("content-length") ?? "0");
  if (length > maximum) throw new Error("upstream response is too large");
  const body = await response.arrayBuffer();
  if (body.byteLength > maximum) throw new Error("upstream response exceeds limit");
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
}

export async function appProxy(request: Request, op: "list" | "start" | "attach" | "detach" | "terminate",
                               sessionId?: string): Promise<Response> {
  const config = xpraAppScope(request, op !== "list");
  if (config instanceof Response) return config;
  if (sessionId !== undefined && !appSessionId(sessionId)) return safeError("Invalid application session.", 400);
  let kind: string | undefined;
  if (op === "start") {
    let raw: unknown;
    try { raw = await boundedJSON(request, 2048); }
    catch { return safeError("Invalid application request.", 400); }
    if (!raw || typeof raw !== "object" || Array.isArray(raw) ||
        Object.keys(raw).join(",") !== "kind" || !validAppKind((raw as {kind?: unknown}).kind))
      return safeError("Only an approved application kind is accepted.", 400);
    kind = (raw as {kind:string}).kind;
  }
  const tail = op === "list" || op === "start" ? "" : `/${sessionId}/${op}`;
  const endpoint = `/v1/xpra/apps${tail}`;
  try {
    const response = await fetch(config.base + endpoint, {
      method: op === "list" ? "GET" : "POST", redirect: "error", cache: "no-store",
      headers: { Authorization: `Bearer ${config.secret}`,
                 ...(op === "start" ? { "Content-Type": "application/json" } : {}) },
      ...(op === "start" ? { body: JSON.stringify({kind}) } : {}),
      signal: AbortSignal.timeout(7000),
    });
    if (response.status === 503) return safeError("Workspace launcher is not configured.", 503);
    if (response.status === 404) return safeError("Application session not found.", 404);
    if (response.status === 409) return safeError("Application request was rejected by the operator.", 409);
    if (!response.ok) return safeError("Application service unavailable.", 502);
    const document = await boundedResponse(response, 45_000);
    const obj = document as {workspaceId?: unknown;session?:unknown};
    if (op === "list") {
      const projected = projectAppInventory(document, config.workspaceId);
      return projected ? Response.json(projected, {headers:{"Cache-Control":"private, no-store"}}) :
        safeError("Malformed application inventory.", 502);
    }
    const session = obj?.workspaceId === config.workspaceId && projectAppSession(obj.session);
    return session ? Response.json({workspaceId:config.workspaceId, session},
      {status:op === "start" ? 201 : 200,headers:{"Cache-Control":"private, no-store"}}) :
      safeError("Malformed application response.", 502);
  } catch { return safeError("Application service unavailable.", 503); }
}
