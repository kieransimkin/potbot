import { xpraBridgeConfig } from "../../../../../lib/server/xpra.ts";
import { requireOperator, safeError, validOrigin } from "../../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Mint a one-use, 30-second Xpra capability after normal UI authentication. */
export async function POST(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (!validOrigin(request, operator)) return safeError("Untrusted Xpra session origin.", 403);
  const config = xpraBridgeConfig();
  if (!config) return safeError("Workspace Xpra stream not configured.", 503);
  if (request.headers.get("x-potbot-workspace") !== null && request.headers.get("x-potbot-workspace") !== config.workspaceId)
    return safeError("Xpra is not configured for the selected workspace.", 404);
  try {
    const result = await fetch(config.base + "/v1/xpra/tickets", {
      method: "POST", redirect: "error", cache: "no-store",
      headers: { Authorization: `Bearer ${config.secret}` },
      signal: AbortSignal.timeout(5000),
    });
    if (!result.ok || Number(result.headers.get("content-length") ?? 0) > 4096) throw new Error();
    const data: unknown = await result.json();
    const record = data as { workspaceId?: unknown; ticket?: unknown; expiresInSeconds?: unknown };
    if (!record || record.workspaceId !== config.workspaceId ||
        typeof record.ticket !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(record.ticket) ||
        record.expiresInSeconds !== 30) throw new Error();
    const ws = new URL(config.publicWs);
    ws.searchParams.set("ticket", record.ticket);
    return Response.json({ workspaceId: config.workspaceId, wsUrl: ws.toString(),
                           expiresInSeconds: 30, transport: "xpra-native-binary" },
                         { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Workspace Xpra session unavailable.", 503); }
}
