import { xpraAppScope } from "./xpra-apps.ts";
import { appSessionId } from "../xpra-apps.ts";
import { boundedJSON, safeError } from "./security.ts";

const ops = new Set(["state", "navigate", "accessibility", "interactive", "inspect.start", "inspect.poll",
  "inspect.stop", "inspect.at", "input.pointer", "input.keyboard", "role.click", "role.fill", "cdp", "sample"]);

async function boundedResponse(response: Response): Promise<unknown> {
  const maximum = 1_510_000;
  if (Number(response.headers.get("content-length") || 0) > maximum) throw Error("Oversized browser response");
  const body = await response.arrayBuffer();
  if (body.byteLength > maximum) throw Error("Oversized browser response");
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
}

/** No arbitrary CDP targets: this route talks only to a leased Xpra browser session. */
export async function browserProxy(request: Request, sessionId: string): Promise<Response> {
  const config = xpraAppScope(request, true);
  if (config instanceof Response) return config;
  if (!appSessionId(sessionId)) return safeError("Invalid browser session.", 400);
  let command: unknown;
  try { command = await boundedJSON(request, 4096); }
  catch { return safeError("Malformed browser command.", 400); }
  if (!command || typeof command !== "object" || Array.isArray(command) ||
      typeof (command as {op?:unknown}).op !== "string" ||
      !ops.has((command as {op:string}).op)) return safeError("Unsupported browser command.", 400);
  try {
    const response = await fetch(`${config.base}/v1/xpra/apps/${sessionId}/browser`, {
      method: "POST", redirect: "error", cache: "no-store", headers: {
        Authorization: `Bearer ${config.secret}`, "Content-Type": "application/json",
      }, body: JSON.stringify(command), signal: AbortSignal.timeout(23000),
    });
    if (response.status === 409 || response.status === 404) return safeError("Browser session rejected the operation.", response.status);
    if (!response.ok) return safeError("Browser service unavailable.", 503);
    const data = await boundedResponse(response) as {workspaceId?: unknown;result?: unknown};
    if (data?.workspaceId !== config.workspaceId || !data.result ||
        typeof data.result !== "object" || Array.isArray(data.result))
      return safeError("Malformed browser service response.", 502);
    return Response.json({ workspaceId: config.workspaceId, result: data.result },
      { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Browser command outcome unavailable; inspect session state.", 503); }
}
