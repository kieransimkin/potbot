/** Strict, origin-scoped wire projection for the native Xpra HTML5 window bridge. */
export const XWINDOW_ID = /^xwin-[a-f0-9]{1,8}$/;
export const WORKSPACE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const XPRA_MAX_WIDTH = 1920;
export const XPRA_MAX_HEIGHT = 1200;

export type XWindowInfo = {
  id: string; title: string; width: number; height: number; x: number; y: number;
  generation: number;
};
export type XWindowTransport = "xpra-native-binary";
export type XpraFrame = {
  id: string; generation: number; sequence: number;
  full: boolean; x: number; y: number; width: number; height: number;
  surfaceWidth: number; surfaceHeight: number;
};
export function windowPanelId(id: string): string {
  if (!XWINDOW_ID.test(id)) throw new Error("Invalid Xpra window identifier");
  return `x11:${id}`;
}
export function validXpraWindow(event: unknown): XWindowInfo | null {
  if (!event || typeof event !== "object" || Array.isArray(event)) return null;
  const o = event as Record<string, unknown>;
  if (typeof o.id !== "string" || !XWINDOW_ID.test(o.id) || typeof o.title !== "string" ||
      o.title.length > 120 || ![o.width, o.height, o.x, o.y, o.generation].every(Number.isSafeInteger) ||
      !(Number(o.width) >= 1 && Number(o.width) <= XPRA_MAX_WIDTH) ||
      !(Number(o.height) >= 1 && Number(o.height) <= XPRA_MAX_HEIGHT) ||
      Math.abs(Number(o.x)) > 32768 || Math.abs(Number(o.y)) > 32768 ||
      Number(o.generation) < 1 || Number(o.generation) > 0x7fffffff) return null;
  return {
    id: o.id, title: o.title, width: Number(o.width), height: Number(o.height),
    x: Number(o.x), y: Number(o.y), generation: Number(o.generation),
  };
}

export function validXpraFrame(value: unknown, bitmap: { width: number; height: number }): XpraFrame | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const o = value as Record<string, unknown>;
  if (typeof o.id !== "string" || !XWINDOW_ID.test(o.id) || typeof o.full !== "boolean" ||
      ![o.generation, o.sequence, o.x, o.y, o.width, o.height,
        o.surfaceWidth, o.surfaceHeight].every(Number.isSafeInteger)) return null;
  const frame = {
    id: o.id, generation: Number(o.generation), sequence: Number(o.sequence),
    x: Number(o.x), y: Number(o.y), width: Number(o.width), height: Number(o.height),
    surfaceWidth: Number(o.surfaceWidth), surfaceHeight: Number(o.surfaceHeight), full: o.full,
  };
  if (frame.generation < 1 || frame.generation > 0x7fffffff || frame.sequence < 1 ||
      frame.surfaceWidth < 1 || frame.surfaceHeight < 1 ||
      frame.surfaceWidth > XPRA_MAX_WIDTH || frame.surfaceHeight > XPRA_MAX_HEIGHT ||
      frame.x < 0 || frame.y < 0 || frame.width < 1 || frame.height < 1 ||
      frame.x + frame.width > frame.surfaceWidth || frame.y + frame.height > frame.surfaceHeight ||
      frame.width !== bitmap.width || frame.height !== bitmap.height ||
      (frame.full && (frame.x !== 0 || frame.y !== 0 ||
        frame.width !== frame.surfaceWidth || frame.height !== frame.surfaceHeight))) return null;
  return frame;
}

/** Never return Xpra service bearer tokens to browser JavaScript. */
export function xpraFrameURL(websocketURL: string, channel: string): string {
  if (!/^[a-f0-9]{32}$/.test(channel)) throw new Error("Invalid Xpra browser channel");
  const ws = new URL(websocketURL);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(ws.hostname);
  if (!(ws.protocol === "ws:" || ws.protocol === "wss:") ||
      (ws.protocol !== "wss:" && !local) || ws.username || ws.password ||
      ws.pathname !== "/v1/xpra/ws" || ws.searchParams.size !== 1 ||
      !/^[A-Za-z0-9_-]{43}$/.test(ws.searchParams.get("ticket") || ""))
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
  | { action: "visibility"; visible: boolean }
  | { action: "resize"; width: number; height: number }
  | { action: "pointer"; x: number; y: number }
  | { action: "button"; x: number; y: number; button: number; pressed: boolean }
  | { action: "wheel"; x: number; y: number; deltaY: number }
  | { action: "key"; key: string; code: string; pressed: boolean;
      shiftKey?: boolean; ctrlKey?: boolean; altKey?: boolean; metaKey?: boolean };
export function validXpraControl(event: XpraControl): boolean {
  if (event.action === "frame" || event.action === "focus" || event.action === "blur") return true;
  if (event.action === "visibility") return typeof event.visible === "boolean";
  if (event.action === "resize") return Number.isSafeInteger(event.width) && Number.isSafeInteger(event.height) &&
    event.width >= 1 && event.width <= XPRA_MAX_WIDTH && event.height >= 1 && event.height <= XPRA_MAX_HEIGHT;
  if (event.action === "key") return event.key.length > 0 && event.key.length <= 40 &&
    event.code.length <= 40 && typeof event.pressed === "boolean";
  if (event.action === "pointer" || event.action === "button" || event.action === "wheel") {
    if (!Number.isSafeInteger(event.x) || !Number.isSafeInteger(event.y) ||
        event.x < 0 || event.y < 0 || event.x > XPRA_MAX_WIDTH || event.y > XPRA_MAX_HEIGHT) return false;
    if (event.action === "wheel") return Number.isFinite(event.deltaY) && Math.abs(event.deltaY) <= 1000;
    if (event.action === "button") return typeof event.pressed === "boolean" &&
      Number.isSafeInteger(event.button) && event.button >= 0 && event.button <= 4;
    return true;
  }
  return false;
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
