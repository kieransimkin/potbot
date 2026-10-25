/** Server-only operator session boundary. Not a multi-user identity provider. */
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

export const COOKIE_NAME = "potbot_ui_session";
const COOKIE_LIFETIME_SECONDS = 8 * 60 * 60;

export type AuthConfig = {
  password: string;
  signingKey: string;
  publicOrigin: string;
};

export function authConfig(env: NodeJS.ProcessEnv = process.env): AuthConfig | null {
  const password = env.POTBOT_UI_PASSWORD;
  const signingKey = env.POTBOT_UI_COOKIE_SECRET;
  const publicOrigin = env.POTBOT_UI_PUBLIC_ORIGIN;
  if (!password || password.length < 12 || !signingKey || signingKey.length < 32 || !publicOrigin) return null;
  try {
    const url = new URL(publicOrigin);
    if (url.username || url.password || url.pathname !== "/" || url.search || url.hash ||
        (url.protocol !== "https:" && !(url.protocol === "http:" &&
        ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))) return null;
    return { password, signingKey, publicOrigin: url.origin };
  } catch { return null; }
}

function mac(payload: string, key: string): string {
  return createHmac("sha256", key).update(payload).digest("base64url");
}

function equalSecret(a: string, b: string): boolean {
  const aa = Buffer.from(a), bb = Buffer.from(b);
  return aa.length === bb.length && timingSafeEqual(aa, bb);
}

export function verifyPassword(input: unknown, config: AuthConfig): boolean {
  return typeof input === "string" && input.length <= 1024 && equalSecret(input, config.password);
}

export function sessionCookie(config: AuthConfig, at = Date.now()): string {
  const payload = `v1.${Math.floor(at / 1000)}.${randomBytes(16).toString("base64url")}`;
  const value = `${payload}.${mac(payload, config.signingKey)}`;
  const secure = config.publicOrigin.startsWith("https:") ? "; Secure" : "";
  return `${COOKIE_NAME}=${value}; Path=/; Max-Age=${COOKIE_LIFETIME_SECONDS}; HttpOnly; SameSite=Strict${secure}`;
}

export function expireSessionCookie(config: AuthConfig): string {
  return `${COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict${config.publicOrigin.startsWith("https:") ? "; Secure" : ""}`;
}

export function authenticated(cookieHeader: string | null, config: AuthConfig, at = Date.now()): boolean {
  const cookie = cookieHeader?.split(";").map(s => s.trim()).find(s => s.startsWith(COOKIE_NAME + "="));
  const value = cookie?.slice(COOKIE_NAME.length + 1) || "";
  if (value.length > 256) return false;
  const segments = value.split(".");
  if (segments.length !== 4 || segments[0] !== "v1" || !/^\d{10,11}$/.test(segments[1]) ||
      !/^[A-Za-z0-9_-]{22}$/.test(segments[2]) || !/^[A-Za-z0-9_-]{43}$/.test(segments[3])) return false;
  const issue = Number(segments[1]), now = Math.floor(at / 1000);
  if (!Number.isSafeInteger(issue) || issue > now + 30 || now - issue > COOKIE_LIFETIME_SECONDS) return false;
  return equalSecret(mac(segments.slice(0, 3).join("."), config.signingKey), segments[3]);
}

export function validOrigin(request: Request, config: AuthConfig): boolean {
  return request.headers.get("origin") === config.publicOrigin &&
    request.headers.get("sec-fetch-site") !== "cross-site";
}

/** Bound request bodies even when transfer encoding omits Content-Length. */
export async function boundedJSON(request: Request, maxBytes: number): Promise<unknown> {
  if (!request.body) throw new Error("Request body is missing");
  const reader = request.body.getReader();
  const parts: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const read = await reader.read();
      if (read.done) break;
      total += read.value.byteLength;
      if (total > maxBytes) throw new Error("Request body exceeds allowed length");
      parts.push(read.value);
    }
  } finally { reader.releaseLock(); }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) { result.set(part, offset); offset += part.length; }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(result));
}

export function safeError(message: string, status: number): Response {
  return Response.json({ error: message }, { status, headers: { "Cache-Control": "no-store" } });
}

export function requireOperator(request: Request): AuthConfig | Response {
  const config = authConfig();
  if (!config) return safeError("Operator UI access is not configured.", 503);
  if (!authenticated(request.headers.get("cookie"), config)) return safeError("Log in to the operator workspace.", 401);
  return config;
}

/** In-memory defence in depth. Production multi-worker deployments need an edge limiter. */
const attempts = new Map<string, { count: number; since: number }>();
export function allowLoginAttempt(clientHint: string, now = Date.now()): boolean {
  const key = clientHint.slice(0, 200);
  const current = attempts.get(key);
  if (!current || now - current.since > 15 * 60_000) {
    if (attempts.size > 1000) attempts.clear();
    attempts.set(key, { count: 1, since: now });
    return true;
  }
  current.count++;
  return current.count <= 10;
}
