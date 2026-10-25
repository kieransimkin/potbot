/** Single-workspace, server-only Potbot connection. Service credentials never enter hydration. */
import { safeError } from "./security.ts";

export type PotbotConfig = { base: string; token: string; workspaceId: string; workspaceName: string; model: string };

export function potbotConfig(env: NodeJS.ProcessEnv = process.env): PotbotConfig | null {
  const token = env.POTBOT_UI_BACKEND_TOKEN, workspaceId = env.POTBOT_UI_WORKSPACE_ID;
  const input = env.POTBOT_UI_BACKEND_URL;
  if (!token || !workspaceId || !input || /[\r\n]/.test(token) || token.length > 4096 ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(workspaceId)) return null;
  try {
    const url = new URL(input);
    // Avoid SSRF and credentials in URL: the first deployment is a local backend.
    if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
        || !url.port || url.username || url.password || url.pathname !== "/" || url.search || url.hash) return null;
    const model = env.POTBOT_UI_MODEL || "potbot";
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}$/.test(model)) return null;
    return { base: url.origin, token, workspaceId, workspaceName: (env.POTBOT_UI_WORKSPACE_NAME || "Potbot").slice(0, 80), model };
  } catch { return null; }
}

/** A bounded set of independently authorized workspace service connections.
 * A workspace selector is never permission to switch one bearer token's RLS
 * identity: each configured entry carries its own scoped credential.
 */
export function workspaceConnections(env: NodeJS.ProcessEnv = process.env): PotbotConfig[] {
  const raw = env.POTBOT_UI_WORKSPACES_JSON;
  if (!raw) {
    const legacy = potbotConfig(env);
    return legacy ? [legacy] : [];
  }
  try {
    if (raw.length > 30_000) return [];
    const items: unknown = JSON.parse(raw);
    if (!Array.isArray(items) || items.length < 1 || items.length > 12) return [];
    const seen = new Set<string>();
    const configs: PotbotConfig[] = [];
    for (const value of items) {
      if (value === null || typeof value !== "object" || Array.isArray(value)) return [];
      const obj = value as Record<string, unknown>;
      if (Object.keys(obj).some(k => !["id", "name", "url", "token", "model"].includes(k)) ||
          typeof obj.id !== "string" || typeof obj.url !== "string" ||
          typeof obj.token !== "string" || typeof obj.name !== "string" ||
          obj.name.trim().length < 1 || obj.name.length > 80 || seen.has(obj.id) ||
          obj.id !== obj.id.toLowerCase() ||
          (obj.model !== undefined && typeof obj.model !== "string")) return [];
      const config = potbotConfig({
        POTBOT_UI_WORKSPACE_ID: obj.id,
        POTBOT_UI_BACKEND_URL: obj.url,
        POTBOT_UI_BACKEND_TOKEN: obj.token,
        POTBOT_UI_WORKSPACE_NAME: obj.name,
        POTBOT_UI_MODEL: typeof obj.model === "string" ? obj.model : "potbot",
      } as NodeJS.ProcessEnv);
      if (!config) return [];
      configs.push(config); seen.add(config.workspaceId);
    }
    return configs;
  } catch { return []; }
}

/** A workspace header is an untrusted selector into the server's allowlist. */
export function potbotConfigForRequest(request: Request, env: NodeJS.ProcessEnv = process.env): PotbotConfig | null {
  const configs = workspaceConnections(env);
  const selected = request.headers.get("x-potbot-workspace");
  if (selected !== null) return configs.find(c => c.workspaceId === selected) ?? null;
  return configs.length === 1 ? configs[0] : null;
}

export async function upstream(
  config: PotbotConfig, path: string, init: RequestInit = {}, timeout = 8000,
): Promise<Response> {
  if (!path.startsWith("/v1/") || path.includes("//") || path.includes(".."))
    throw new Error("Unapproved upstream path");
  return fetch(config.base + path, {
    ...init, redirect: "error", cache: "no-store",
    headers: { Accept: "application/json", Authorization: `Bearer ${config.token}`, ...init.headers },
    signal: init.signal ?? AbortSignal.timeout(timeout),
  });
}

export function upstreamFailure(response: Response): Response {
  // No reflection of upstream error bodies or tokens to browser.
  const status = response.status === 401 || response.status === 403 ? 502 :
    response.status === 404 ? 404 : response.status === 503 ? 503 : 502;
  return safeError(status === 404 ? "Resource not found in this workspace." :
    "Potbot backend could not complete this request.", status);
}

export function configuredWorkspace(config: PotbotConfig) {
  return {
    id: config.workspaceId, name: config.workspaceName,
    shortName: config.workspaceName.slice(0, 1).toUpperCase() || "P",
    accent: "#59d4ef", summary: "Authorized Potbot workspace", status: "idle" as const,
  };
}
