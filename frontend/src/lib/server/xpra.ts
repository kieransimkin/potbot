/** Next.js server-only gate to the local Xpra WebSocket relay. */
import { potbotConfig } from "./potbot.ts";

export type XpraBridgeConfig = { base: string; secret: string; publicWs: string; workspaceId: string };
export function xpraBridgeConfig(env: NodeJS.ProcessEnv = process.env): XpraBridgeConfig | null {
  const potbot = potbotConfig(env);
  const address = env.POTBOT_UI_XPRA_BRIDGE_URL;
  const secret = env.POTBOT_UI_XPRA_BRIDGE_TOKEN;
  const publicWs = env.POTBOT_UI_XPRA_PUBLIC_WS_URL;
  if (!potbot || !address || !secret || !publicWs || secret.length < 32 || secret.length > 4096 ||
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
    return { base: local.origin, secret, publicWs: publicEndpoint.toString(), workspaceId: potbot.workspaceId };
  } catch { return null; }
}
