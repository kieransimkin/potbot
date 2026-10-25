import { allowLoginAttempt, boundedJSON, authConfig, safeError, sessionCookie, validOrigin, verifyPassword } from "../../../../../lib/server/security.ts";

export const runtime = "nodejs";
export async function POST(request: Request): Promise<Response> {
  const config = authConfig();
  if (!config) return safeError("Operator UI is not configured.", 503);
  if (!validOrigin(request, config)) return safeError("Untrusted request origin.", 403);
  const limit = Number(request.headers.get("content-length") || "0");
  if (limit > 2048) return safeError("Login request is too large.", 413);
  if (!allowLoginAttempt("global")) return safeError("Too many login attempts; retry later.", 429);
  let body: unknown;
  try { body = await boundedJSON(request, 2048); } catch { return safeError("Invalid login request.", 400); }
  const password = typeof body === "object" && body !== null && "password" in body ?
    (body as { password?: unknown }).password : null;
  if (!verifyPassword(password, config)) return safeError("Invalid operator credentials.", 401);
  return Response.json({ authenticated: true }, { headers: {
    "Cache-Control": "no-store", "Set-Cookie": sessionCookie(config),
  } });
}
