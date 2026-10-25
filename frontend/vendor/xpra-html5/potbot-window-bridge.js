/* Potbot per-window bridge for Xpra-org/xpra-html5 (MPL-2.0 integration).
 * One upstream Xpra client decodes window damage in an isolated iframe.
 * Only ImageBitmaps + sanitized window events reach the parent Dockview UI.
 * Key/pointer/resize/focus commands return through the upstream Xpra client.
 * This script is shipped with Potbot; original upstream files retain MPL headers.
 */
(() => {
  "use strict";
  const channel = new URLSearchParams(location.search).get("potbot_channel") || "";
  const reChannel = /^[a-f0-9]{32}$/;
  if (!reChannel.test(channel) || window.parent === window) return;
  let xpra = null;
  let live = true;
  const lastFrameAt = new Map();
  const pending = new Set();
  const maxWidth = 1920, maxHeight = 1200;
  const emit = (action, values = {}, bitmap) => {
    const message = { type: "potbot:xpra", action, channel, ...values };
    if (bitmap) message.bitmap = bitmap;
    try {
      if (bitmap) window.parent.postMessage(message, location.origin, [bitmap]);
      else window.parent.postMessage(message, location.origin);
    } catch {
      if (bitmap) bitmap.close();
    }
  };
  const identifier = wid => Number.isSafeInteger(wid) && wid > 0 && wid <= 0xffffffff
    ? `xwin-${wid.toString(16)}` : null;
  const publish = win => {
    const id = identifier(win.wid);
    if (!id || !win.canvas || win.tray || win.client.server_is_desktop) return;
    emit("open", {
      id, title: String(win.title || "Xpra application").slice(0, 120),
      width: Math.min(maxWidth, Math.max(1, Math.floor(win.w))),
      height: Math.min(maxHeight, Math.max(1, Math.floor(win.h))),
      x: Math.trunc(win.x) || 0, y: Math.trunc(win.y) || 0,
    });
  };
  const frame = win => {
    const id = identifier(win.wid), now = performance.now();
    if (!live || !id || !win.canvas || pending.has(id) ||
        now - (lastFrameAt.get(id) || 0) < 30) return;
    pending.add(id);
    lastFrameAt.set(id, now);
    // This is a per-Xpra-window decoded canvas, not the virtual desktop image.
    createImageBitmap(win.canvas).then(bitmap => {
      if (!live || !xpra || xpra.id_to_window[win.wid] !== win) bitmap.close();
      else emit("frame", { id, width: bitmap.width, height: bitmap.height }, bitmap);
    }).catch(() => emit("error", { id, reason: "frame-decode-unavailable" }))
      .finally(() => pending.delete(id));
  };
  // Wrap the pinned upstream native methods, never invent XTEST or DOM input
  // devices. Every event is scoped to the owning Xpra window object.
  const originalNew = XpraClient.prototype._new_window;
  XpraClient.prototype._new_window = function (...args) {
    const value = originalNew.apply(this, args);
    const win = this.id_to_window[args[0]];
    if (win) publish(win);
    return value;
  };
  const originalRemove = XpraClient.prototype._process_lost_window;
  XpraClient.prototype._process_lost_window = function (packet) {
    const id = identifier(packet[1]);
    const value = originalRemove.call(this, packet);
    if (id) { lastFrameAt.delete(id); emit("close", { id }); }
    return value;
  };
  const originalMetadata = XpraClient.prototype._process_window_metadata;
  XpraClient.prototype._process_window_metadata = function (packet) {
    const value = originalMetadata.call(this, packet);
    const win = this.id_to_window[packet[1]];
    if (win) publish(win);
    return value;
  };
  const originalDraw = XpraWindow.prototype.draw;
  XpraWindow.prototype.draw = function (...args) {
    const value = originalDraw.apply(this, args);
    frame(this);
    return value;
  };
  window.PotbotXpraBridge = {
    bindClient(client) { xpra = client; client.reconnect = false; },
    onClose() { live = false; emit("disconnected"); },
    ready() { live = true; emit("ready"); },
  };
  // Messages can come only from this iframe's owning same-origin Potbot app.
  window.addEventListener("message", event => {
    if (event.source !== window.parent || event.origin !== location.origin) return;
    const command = event.data;
    if (!command || command.type !== "potbot:xpra-control" || command.channel !== channel) return;
    if (!xpra || !xpra.connected) return;
    const match = /^xwin-([a-f0-9]{1,8})$/.exec(command.id || "");
    if (!match) return;
    const wid = parseInt(match[1], 16);
    const win = xpra.id_to_window[wid];
    if (!win || !win.canvas) return;
    if (command.action === "frame") { lastFrameAt.delete(command.id); frame(win); return; }
    if (command.action === "focus") { xpra.set_focus(win); return; }
    if (command.action === "blur") { xpra.release_keys(); return; }
    if (command.action === "resize") {
      const w = command.width, h = command.height;
      if (!Number.isSafeInteger(w) || !Number.isSafeInteger(h) || w < 1 || h < 1 || w > maxWidth || h > maxHeight) return;
      if (Math.abs(win.w - w) < 2 && Math.abs(win.h - h) < 2) return;
      win.resize(w, h, win.resize_counter + 1);
      xpra.send_configure_window(win, {}, false);
      return;
    }
    if (command.action === "key") {
      if (typeof command.key !== "string" || command.key.length > 40 || typeof command.pressed !== "boolean") return;
      const eventType = command.pressed ? "keydown" : "keyup";
      const e = new KeyboardEvent(eventType, {
        key: command.key, code: command.code || "", bubbles: true,
        ctrlKey: command.ctrlKey === true, shiftKey: command.shiftKey === true,
        altKey: command.altKey === true, metaKey: command.metaKey === true,
      });
      xpra.set_focus(win);
      if (command.pressed) xpra._keyb_onkeydown(e);
      else xpra._keyb_onkeyup(e);
      return;
    }
    if (["pointer", "button", "wheel"].includes(command.action)) {
      const x = command.x, y = command.y;
      if (!Number.isSafeInteger(x) || !Number.isSafeInteger(y) ||
          x < 0 || y < 0 || x >= win.w || y >= win.h) return;
      const rect = win.canvas.getBoundingClientRect();
      const props = { bubbles: true, clientX: rect.left + x, clientY: rect.top + y,
        button: Number.isSafeInteger(command.button) ? command.button : 0,
        buttons: command.pressed ? 1 : 0 };
      if (command.action === "wheel") {
        if (!Number.isFinite(command.deltaY) || Math.abs(command.deltaY) > 1000) return;
        xpra.on_mousescroll(new WheelEvent("wheel", {...props, deltaY: command.deltaY}), win);
      } else {
        const type = command.action === "pointer" ? "mousemove" :
          command.pressed ? "mousedown" : "mouseup";
        const e = new MouseEvent(type, props);
        if (command.action === "pointer") xpra.on_mousemove(e, win);
        else if (command.pressed) xpra.on_mousedown(e, win);
        else xpra.on_mouseup(e, win);
      }
    }
  });
  window.addEventListener("pagehide", () => { live = false; });
})();
