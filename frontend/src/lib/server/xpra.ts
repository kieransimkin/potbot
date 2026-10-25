/** Next.js server-only gate to the local Xpra WebSocket relay. */
import { workspaceConnections } from "./potbot.ts";

export type XpraBridgeConfig = { base: string; secret: string; publicWs: string; workspaceId: string };
export function xpraBridgeConfig(env: NodeJS.ProcessEnv = process.env): XpraBridgeConfig | null {
  const configs = workspaceConnections(env);
  const requested = env.POTBOT_UI_XPRA_WORKSPACE_ID;
  const potbot = requested ? configs.find(c => c.workspaceId === requested) :
    configs.length === 1 ? configs[0] : undefined;
  return potbot ? validatedBridge(potbot.workspaceId, env.POTBOT_UI_XPRA_BRIDGE_URL,
    env.POTBOT_UI_XPRA_BRIDGE_TOKEN, env.POTBOT_UI_XPRA_PUBLIC_WS_URL) : null;
}

function validatedBridge(workspaceId: string, address: unknown, secret: unknown, publicWs: unknown): XpraBridgeConfig | null {
  if (typeof address !== "string" || typeof secret !== "string" || typeof publicWs !== "string" ||
      !address || !secret || !publicWs || secret.length < 32 || secret.length > 4096 ||
      /[\r\n]/.test(secret)) return null;
  try {
    const local = new URL(address);
    const publicEndpoint = new URL(publicWs);
    const wsLocal = ["localhost", "127.0.0.1", "[::1]"].includes(publicEndpoint.hostname);
    if (local.protocol !== "http:" || !["localhost", "127.0.0.1", "[::1]"].includes(local.hostname) ||
        !local.port || local.pathname !== "/" || local.username || local.password || local.search || local.hash ||
        (publicEndpoint.protocol !== "wss:" && !(publicEndpoint.protocol === "ws:" && wsLocal)) ||
        publicEndpoint.pathname !== "/v1/xpra/ws" || publicEndpoint.search || publicEndpoint.hash ||
        publicEndpoint.username || publicEndpoint.password) return null;
    return { base: local.origin, secret, publicWs: publicEndpoint.toString(), workspaceId };
  } catch { return null; }
}

/** Multi-workspace Xpra is configured exclusively on the Next.js host.
 * The user-selected workspace must be on the already-authorized backend list.
 * Legacy single-bridge environment settings continue to work unchanged.
 */
export function xpraBridgeConfigForWorkspace(workspaceId: string | null,
    env: NodeJS.ProcessEnv = process.env): XpraBridgeConfig | null {
  if (!workspaceId) return env.POTBOT_UI_XPRA_BRIDGES_JSON ? null : xpraBridgeConfig(env);
  const allowed = workspaceConnections(env);
  if (!allowed.some(x => x.workspaceId === workspaceId)) return null;
  const serialized = env.POTBOT_UI_XPRA_BRIDGES_JSON;
  if (serialized === undefined) {
    const legacy = xpraBridgeConfig(env);
    return legacy?.workspaceId === workspaceId ? legacy : null;
  }
  if (serialized.length > 16_384) return null;
  try {
    const entries: unknown = JSON.parse(serialized);
    if (!Array.isArray(entries) || !entries.length || entries.length > 20) return null;
    const seen = new Set<string>();
    let match: XpraBridgeConfig | null = null;
    for (const item of entries) {
      if (!item || typeof item !== "object" || Array.isArray(item)) return null;
      const x = item as Record<string, unknown>;
      if (Object.keys(x).sort().join(",") !== "bridgeToken,bridgeUrl,publicWsUrl,workspaceId" ||
          typeof x.workspaceId !== "string" || seen.has(x.workspaceId) ||
          !allowed.some(w => w.workspaceId === x.workspaceId)) return null;
      seen.add(x.workspaceId);
      const bridge = validatedBridge(x.workspaceId, x.bridgeUrl, x.bridgeToken, x.publicWsUrl);
      if (!bridge) return null;
      if (x.workspaceId === workspaceId) match = bridge;
    }
    return match;
  } catch { return null; }
}
