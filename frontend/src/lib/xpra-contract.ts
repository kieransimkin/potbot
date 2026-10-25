/** Validated window events for native Xpra HTML5, never raw X11 socket metadata. */
export const XWINDOW_ID = /^xwin-[a-f0-9]{1,8}$/;
export const WORKSPACE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export type XWindowInfo = {
  id: string; title: string; width: number; height: number; x: number; y: number;
};
export type XWindowTransport = "xpra-native-binary";
export function windowPanelId(id: string): string {
  if (!XWINDOW_ID.test(id)) throw new Error("Invalid Xpra window identifier");
  return `x11:${id}`;
}
export function validXpraWindow(event: unknown): XWindowInfo | null {
  if (!event || typeof event !== "object" || Array.isArray(event)) return null;
  const o = event as Record<string, unknown>;
  if (typeof o.id !== "string" || !XWINDOW_ID.test(o.id) || typeof o.title !== "string" ||
      o.title.length > 120 || ![o.width, o.height, o.x, o.y].every(Number.isSafeInteger) ||
      !(Number(o.width) >= 1 && Number(o.width) <= 1920) ||
      !(Number(o.height) >= 1 && Number(o.height) <= 1200) ||
      Math.abs(Number(o.x)) > 32768 || Math.abs(Number(o.y)) > 32768) return null;
  return { id: o.id, title: o.title, width: Number(o.width), height: Number(o.height),
    x: Number(o.x), y: Number(o.y) };
}
export function xpraFrameURL(websocketURL: string, channel: string): string {
  if (!/^[a-f0-9]{32}$/.test(channel)) throw new Error("Invalid Xpra browser channel");
  const ws = new URL(websocketURL);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(ws.hostname);
  if (!(["ws:", "wss:"].includes(ws.protocol)) ||
      (ws.protocol !== "wss:" && !local) || ws.username || ws.password ||
      ws.pathname !== "/v1/xpra/ws" || !/^[A-Za-z0-9_-]{43}$/.test(ws.searchParams.get("ticket") || ""))
    throw new Error("Invalid Xpra WebSocket ticket endpoint");
  const params = new URLSearchParams({
    server: ws.hostname.replace(/^\[|\]$/g, ""), port: ws.port || (ws.protocol === "wss:" ? "443" : "80"),
    ssl: ws.protocol === "wss:" ? "true" : "false",
    path: ws.pathname + ws.search, action: "connect", floating_menu: "false",
    clipboard: "false", printing: "false", file_transfer: "false", sound: "false",
    video: "false", offscreen: "false", reconnect: "false", remote_logging: "false",
    potbot_channel: channel,
  });
  return `/xpra-html5/index.html?${params.toString()}`;
}
export type XpraControl =
  | { action: "frame" | "focus" | "blur" }
  | { action: "resize"; width: number; height: number }
  | { action: "pointer" | "button"; x: number; y: number; button?: number; pressed?: boolean }
  | { action: "wheel"; x: number; y: number; deltaY: number }
  | { action: "key"; key: string; code: string; pressed: boolean;
      shiftKey?: boolean; ctrlKey?: boolean; altKey?: boolean; metaKey?: boolean };
export function validXpraControl(event: XpraControl): boolean {
  if (["frame", "focus", "blur"].includes(event.action)) return true;
  if (event.action === "resize") return Number.isSafeInteger(event.width) && Number.isSafeInteger(event.height) &&
    event.width >= 1 && event.width <= 1920 && event.height >= 1 && event.height <= 1200;
  if (event.action === "key") return event.key.length <= 40 && event.code.length <= 40 &&
    typeof event.pressed === "boolean";
  if (!["pointer", "button", "wheel"].includes(event.action)) return false;
  if (!Number.isSafeInteger(event.x) || !Number.isSafeInteger(event.y) ||
      event.x < 0 || event.y < 0 || event.x > 1920 || event.y > 1200) return false;
  if (event.action === "wheel") return Number.isFinite(event.deltaY) && Math.abs(event.deltaY) <= 1000;
  if (event.action === "button") return typeof event.pressed === "boolean" &&
    Number.isSafeInteger(event.button) && (event.button ?? -1) >= 0 && (event.button ?? 9) <= 4;
  return true;
}
export function normalizedPointerPosition(clientX: number, clientY: number, bounds: DOMRect,
                                          naturalWidth: number, naturalHeight: number): { x: number; y: number } | null {
  if (naturalWidth < 1 || naturalHeight < 1 || bounds.width <= 0 || bounds.height <= 0) return null;
  const scale = Math.min(bounds.width / naturalWidth, bounds.height / naturalHeight);
  const x = (clientX - bounds.left - (bounds.width - naturalWidth * scale) / 2) / scale;
  const y = (clientY - bounds.top - (bounds.height - naturalHeight * scale) / 2) / scale;
  if (x < 0 || y < 0 || x >= naturalWidth || y >= naturalHeight) return null;
  return { x: Math.floor(x), y: Math.floor(y) };
}
