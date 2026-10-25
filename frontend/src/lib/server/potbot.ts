/** Single-workspace, server-only Potbot connection. Service credentials never enter hydration. */
import { safeError } from "./security.ts";

type PotbotConfig = { base: string; token: string; workspaceId: string; workspaceName: string; model: string };

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
