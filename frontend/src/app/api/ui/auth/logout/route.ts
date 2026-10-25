import { authConfig, expireSessionCookie, safeError, validOrigin } from "../../../../../lib/server/security.ts";

export const runtime = "nodejs";
export function POST(request: Request): Response {
  const config = authConfig();
  if (!config) return safeError("Operator UI is not configured.", 503);
  if (!validOrigin(request, config)) return safeError("Untrusted request origin.", 403);
  return Response.json({ authenticated: false }, { headers: {
    "Cache-Control": "no-store", "Set-Cookie": expireSessionCookie(config),
  } });
}
