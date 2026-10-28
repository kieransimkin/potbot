/** Operator BFF: agent display streaming is observation only, never browser control. */
import { xpraAppScope } from "./xpra-apps.ts";
import { agentXpraSessions } from "../agent-xpra-contract.ts";
import { safeError } from "./security.ts";
import { appSessionId } from "../xpra-apps.ts";
import { WORKSPACE_ID } from "../xpra-contract.ts";

const NO_STORE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"};
async function boundedJson(response: Response, bound: number): Promise<unknown> {
  if (Number(response.headers.get("content-length") || "0") > bound) throw Error("Oversize");
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > bound) throw Error("Oversize");
  return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));
}

export async function agentXpraInventoryProxy(request: Request): Promise<Response> {
  const config = xpraAppScope(request, false);
  if (config instanceof Response) return config;
  try {
    const response = await fetch(`${config.base}/v1/xpra/agent-sessions`, {
      method: "GET", redirect: "error", cache: "no-store",
      headers: {Authorization: `Bearer ${config.secret}`}, signal: AbortSignal.timeout(6000),
    });
    if (!response.ok) return safeError("Agent Xpra inventory unavailable.", response.status === 503 ? 503 : 502);
    const values = agentXpraSessions(await boundedJson(response, 12000), config.workspaceId);
    return values === null ? safeError("Invalid agent Xpra inventory.", 502) :
      Response.json({workspaceId: config.workspaceId, viewOnly: true, sessions: values}, {headers: NO_STORE});
  } catch { return safeError("Agent Xpra inventory disconnected.", 503); }
}

export async function agentXpraTicketProxy(request: Request, agentId: string, sessionId: string): Promise<Response> {
  const config = xpraAppScope(request, true);
  if (config instanceof Response) return config;
  if (!WORKSPACE_ID.test(agentId) || agentId !== agentId.toLowerCase() || !appSessionId(sessionId) ||
      (request.headers.get("content-length") && request.headers.get("content-length") !== "0"))
    return safeError("Invalid agent browser scope.", 400);
  // This endpoint accepts no user-supplied browser command, URL, or destination.
  try {
    const response = await fetch(`${config.base}/v1/xpra/agent-sessions/${agentId}/${sessionId}/ticket`, {
      method: "POST", redirect: "error", cache: "no-store",
      headers: {Authorization: `Bearer ${config.secret}`}, signal: AbortSignal.timeout(6000),
    });
    if (!response.ok) return safeError("Agent display is not available for viewing.", response.status === 404 ? 404 : 503);
    const value = await boundedJson(response, 4096) as Record<string, unknown>;
    if (value?.workspaceId !== config.workspaceId || typeof value.ticket !== "string" ||
        !/^[A-Za-z0-9_-]{43}$/.test(value.ticket) || value.expiresInSeconds !== 30 ||
        value.transport !== "xpra-native-binary" || value.viewOnly !== true)
      throw Error("Invalid operator ticket");
    const ws = new URL(config.publicWs);
    ws.pathname = "/v1/xpra/agent/ws";
    ws.search = "";
    ws.searchParams.set("ticket", value.ticket);
    return Response.json({workspaceId: config.workspaceId, agentId, sessionId,
      wsUrl: ws.toString(), expiresInSeconds: 30, transport: "xpra-native-binary", viewOnly: true},
      {headers: NO_STORE});
  } catch { return safeError("Agent Xpra stream admission failed.", 503); }
}
