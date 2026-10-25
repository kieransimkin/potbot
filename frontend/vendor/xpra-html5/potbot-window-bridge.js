/* Potbot per-window Xpra HTML5 adapter (MPL-2.0 integration).
 * The upstream client retains its native Xpra decoder and input protocol.
 * Potbot's Dockview owns the visible chrome; window image damage is forwarded
 * as cropped ImageBitmaps, never a screenshot of the virtual desktop.
 */
(() => {
  "use strict";
  const channel = new URLSearchParams(location.search).get("potbot_channel") || "";
  if (!/^[a-f0-9]{32}$/.test(channel) || window.parent === window) return;
  const MAX_W = 1920, MAX_H = 1200, FRAME_MS = 34;
  let xpra = null, live = true, nextGeneration = 0;
  const states = new Map();
  const identifier = wid => Number.isSafeInteger(wid) && wid > 0 && wid <= 0xffffffff
    ? `xwin-${wid.toString(16)}` : null;
  const emit = (action, payload = {}, bitmap) => {
    const message = { type: "potbot:xpra", channel, action, ...payload };
    if (bitmap) message.bitmap = bitmap;
    try {
      if (bitmap) window.parent.postMessage(message, location.origin, [bitmap]);
      else window.parent.postMessage(message, location.origin);
    } catch {
      bitmap?.close();
    }
  };
  const dimensions = win => ({ width: Math.max(1, Math.floor(win.canvas?.width || win.w || 1)),
    height: Math.max(1, Math.floor(win.canvas?.height || win.h || 1)) });
  const stateFor = win => states.get(identifier(win.wid));
  const publish = win => {
    const id = identifier(win.wid), state = stateFor(win);
    if (!id || !state || !win.canvas || win.tray || win.client.server_is_desktop) return;
    emit("open", { id, generation: state.generation,
      title: String(win.title || "Xpra application").slice(0, 120),
      width: Math.min(MAX_W, Math.max(1, Math.floor(win.w))),
      height: Math.min(MAX_H, Math.max(1, Math.floor(win.h))),
      x: Math.trunc(win.x) || 0, y: Math.trunc(win.y) || 0 });
  };
  function markFull(state) { state.full = true; state.dirty = null; }
  function markDamage(win, packet) {
    const state = stateFor(win);
    if (!state || state.full) return;
    const { width: sw, height: sh } = dimensions(win);
    if (sw > MAX_W || sh > MAX_H || (typeof packet[6] === "string" ? packet[6] : Utilities.s(packet[6])) === "scroll" ||
        ![packet[2], packet[3], packet[4], packet[5]].every(Number.isSafeInteger)) {
      markFull(state); return;
    }
    const [x, y, w, h] = packet.slice(2, 6);
    // Any out-of-bounds, tiled/scroll or unusual update receives a full sync.
    if (w < 1 || h < 1 || x < 0 || y < 0 || x + w > sw || y + h > sh) {
      markFull(state); return;
    }
    if (!state.dirty) state.dirty = { x, y, w, h };
    else {
      const old = state.dirty;
      const l = Math.min(old.x, x), t = Math.min(old.y, y);
      state.dirty = { x: l, y: t,
        w: Math.max(old.x + old.w, x + w) - l,
        h: Math.max(old.y + old.h, y + h) - t };
    }
    if (state.dirty.w * state.dirty.h >= sw * sh * .70) markFull(state);
  }
  function requestFrame(win) {
    const id = identifier(win.wid), state = states.get(id);
    if (!live || !xpra?.connected || !id || !state || !state.visible ||
        xpra.id_to_window[win.wid] !== win || !win.canvas ||
        (!state.full && !state.dirty) || state.pending || state.blocked) return;
    const width = win.canvas.width, height = win.canvas.height;
    if (![width, height].every(Number.isSafeInteger) || width < 1 || height < 1 ||
        width > MAX_W || height > MAX_H) return;
    const now = performance.now();
    const wait = Math.ceil(state.lastFrameAt + FRAME_MS - now);
    if (wait > 1) {
      if (!state.timer) state.timer = setTimeout(() => {
        state.timer = null; requestFrame(win);
      }, wait);
      return;
    }
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    const full = state.full;
    const rect = full ? { x: 0, y: 0, w: width, h: height } : state.dirty;
    if (!rect || rect.x + rect.w > width || rect.y + rect.h > height) {
      markFull(state); return requestFrame(win);
    }
    state.full = false;
    state.dirty = null;
    state.pending = true;
    state.lastFrameAt = now;
    const sequence = ++state.sequence;
    const generation = state.generation;
    // Cropping here avoids copying a full window for a 20x20 changed region.
    Promise.resolve().then(() => createImageBitmap(win.canvas,
      rect.x, rect.y, rect.w, rect.h)).then(bitmap => {
      if (!live || !state.visible || states.get(id) !== state ||
          xpra.id_to_window[win.wid] !== win) { bitmap.close(); return; }
      state.failures = 0;
      emit("frame", { id, generation, sequence, full,
        x: rect.x, y: rect.y, width: rect.w, height: rect.h,
        surfaceWidth: width, surfaceHeight: height }, bitmap);
    }).catch(() => {
      markFull(state);
      state.failures++;
      if (state.failures >= 2) {
        state.blocked = true;
        emit("error", { id, generation, reason: "window-bitmap-unavailable" });
      }
    }).finally(() => {
      state.pending = false;
      if (state.visible && (state.full || state.dirty)) requestFrame(win);
    });
  }
  function visibility(win, visible) {
    const state = stateFor(win);
    if (!state) return;
    state.visible = visible;
    if (!visible) {
      if (state.timer) { clearTimeout(state.timer); state.timer = null; }
      markFull(state);
    } else {
      state.blocked = false;
      state.failures = 0;
      markFull(state);
      requestFrame(win);
    }
  }
  // Pinned Xpra HTML5 hooks. The preparation script verifies these methods.
  const originalNew = XpraClient.prototype._new_window;
  XpraClient.prototype._new_window = function (...args) {
    const result = originalNew.apply(this, args), wid = args[0];
    const win = this.id_to_window[wid], id = identifier(wid);
    if (win && id) {
      states.set(id, { generation: ++nextGeneration, sequence: 0, visible: false,
        full: true, dirty: null, pending: false, timer: null, lastFrameAt: -FRAME_MS,
        failures: 0, blocked: false });
      publish(win);
    }
    return result;
  };
  const originalLost = XpraClient.prototype._process_lost_window;
  XpraClient.prototype._process_lost_window = function (packet) {
    const id = identifier(packet[1]), state = states.get(id);
    const result = originalLost.call(this, packet);
    if (state?.timer) clearTimeout(state.timer);
    if (id && state) { states.delete(id); emit("close", { id, generation: state.generation }); }
    return result;
  };
  const originalMetadata = XpraClient.prototype._process_window_metadata;
  XpraClient.prototype._process_window_metadata = function (packet) {
    const result = originalMetadata.call(this, packet);
    const win = this.id_to_window[packet[1]];
    if (win) publish(win);
    return result;
  };
  const originalResize = XpraWindow.prototype.resize;
  XpraWindow.prototype.resize = function (...args) {
    const oldWidth = this.w, oldHeight = this.h;
    const result = originalResize.apply(this, args);
    if (this.w !== oldWidth || this.h !== oldHeight) {
      const state = stateFor(this);
      if (state) markFull(state);
      publish(this);
    }
    return result;
  };
  const originalPaint = XpraWindow.prototype.do_paint;
  XpraWindow.prototype.do_paint = function (packet, callback) {
    return originalPaint.call(this, packet, (error) => {
      if (!error) markDamage(this, packet);
      return callback(error);
    });
  };
  const originalDraw = XpraWindow.prototype.draw;
  XpraWindow.prototype.draw = function (...args) {
    const result = originalDraw.apply(this, args);
    requestFrame(this);
    return result;
  };
  window.PotbotXpraBridge = {
    bindClient(client) { xpra = client; client.reconnect = false; },
    onClose() { live = false; for (const state of states.values()) {
      if (state.timer) clearTimeout(state.timer);
      state.visible = false;
    } emit("disconnected"); },
    ready() { live = true; emit("ready"); },
  };
  window.addEventListener("message", event => {
    if (event.source !== window.parent || event.origin !== location.origin) return;
    const command = event.data;
    if (!command || command.type !== "potbot:xpra-control" || command.channel !== channel ||
        !xpra?.connected) return;
    const match = /^xwin-([a-f0-9]{1,8})$/.exec(command.id || "");
    if (!match) return;
    const wid = parseInt(match[1], 16), win = xpra.id_to_window[wid];
    if (!win || !win.canvas || !stateFor(win)) return;
    if (command.action === "visibility") {
      if (typeof command.visible === "boolean") visibility(win, command.visible);
      return;
    }
    if (command.action === "frame") {
      const state = stateFor(win);
      state.blocked = false;
      state.failures = 0;
      markFull(state); requestFrame(win); return;
    }
    if (command.action === "focus") { xpra.set_focus(win); return; }
    if (command.action === "blur") { xpra.release_keys(); return; }
    if (command.action === "resize") {
      const w = command.width, h = command.height;
      if (!Number.isSafeInteger(w) || !Number.isSafeInteger(h) ||
          w < 1 || h < 1 || w > MAX_W || h > MAX_H) return;
      if (Math.abs(win.w - w) < 2 && Math.abs(win.h - h) < 2) return;
      markFull(stateFor(win));
      win.resize(w, h, win.resize_counter + 1);
      xpra.send_configure_window(win, {}, false);
      return;
    }
    if (command.action === "key") {
      if (typeof command.key !== "string" || !command.key || command.key.length > 40 ||
          typeof command.code !== "string" || command.code.length > 40 ||
          typeof command.pressed !== "boolean") return;
      const type = command.pressed ? "keydown" : "keyup";
      const e = new KeyboardEvent(type, {
        key: command.key, code: command.code, bubbles: true,
        ctrlKey: command.ctrlKey === true, shiftKey: command.shiftKey === true,
        altKey: command.altKey === true, metaKey: command.metaKey === true,
      });
      xpra.set_focus(win);
      if (command.pressed) xpra._keyb_onkeydown(e);
      else xpra._keyb_onkeyup(e);
      return;
    }
    if (!["pointer", "button", "wheel"].includes(command.action)) return;
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
  });
  window.addEventListener("pagehide", () => { live = false; });
})();
