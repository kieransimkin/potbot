"use client";

import { useEffect, useRef } from "react";
import { attachXpraCanvas } from "@/lib/xpra-frames";
import { normalizedPointerPosition, XWINDOW_ID } from "@/lib/xpra-contract";
import { useWorkspace } from "./WorkspaceContext";

/** Each Dockview surface owns its own Xpra window canvas, not a desktop iframe. */
export function XpraWindowPanel({ params }: { params?: { windowId?: string } }) {
  const { xWindows, closeXWindow, xAvailable, sendXpra } = useWorkspace();
  const id = params?.windowId || "";
  const info = xWindows.find(x => x.id === id);
  const canvas = useRef<HTMLCanvasElement>(null);
  const lastPointer = useRef(0);
  useEffect(() => {
    const surface = canvas.current;
    if (!surface || !XWINDOW_ID.test(id)) return;
    const unregister = attachXpraCanvas(id, surface);
    sendXpra(id, { action: "frame" });
    sendXpra(id, { action: "focus" });
    // Dockview owns the outer geometry; ask Xpra to resize its client window.
    const observer = new ResizeObserver(entries => {
      const target = entries[0]?.contentRect;
      if (!target) return;
      const width = Math.min(1920, Math.round(target.width));
      const height = Math.min(1200, Math.round(target.height));
      if (width > 64 && height > 64) sendXpra(id, { action: "resize", width, height });
    });
    observer.observe(surface.parentElement || surface);
    return () => { observer.disconnect(); unregister(); };
  }, [id, sendXpra]);
  function pointer(event: React.PointerEvent<HTMLCanvasElement>, action: "pointer" | "button", pressed = false) {
    if (!canvas.current || !info) return;
    if (action === "pointer" && performance.now() - lastPointer.current < 16) return;
    lastPointer.current = performance.now();
    const point = normalizedPointerPosition(event.clientX, event.clientY,
      canvas.current.getBoundingClientRect(), info.width, info.height);
    if (!point) return;
    if (action === "pointer") sendXpra(id, { action, ...point });
    else { sendXpra(id, { action, ...point, button: event.button, pressed });
      if (pressed) { event.currentTarget.setPointerCapture(event.pointerId); event.currentTarget.focus(); sendXpra(id, { action: "focus" }); } }
  }
  return <section className="xwindow-panel" aria-label={`Xpra application ${info?.title || id}`}>
    <header className="xwindow-toolbar">
      <strong title={info?.title}>{info?.title || "Xpra window"}</strong>
      <small>Native Xpra stream · Dockview chrome</small>
      <button type="button" onClick={() => closeXWindow(id)} aria-label="Minimize application pane">Minimize ↗</button>
    </header>
    <div className="xwindow-viewport">
      <canvas ref={canvas} width={info?.width || 640} height={info?.height || 480}
        className="xpra-app-canvas" aria-label={`Interactive Xpra window: ${info?.title || id}`}
        role="img" tabIndex={0}
        onPointerDown={e => pointer(e, "button", true)}
        onPointerUp={e => pointer(e, "button", false)}
        onPointerMove={e => pointer(e, "pointer")}
        onContextMenu={e => e.preventDefault()}
        onWheel={e => { if (!info || !canvas.current) return;
          const coords = normalizedPointerPosition(e.clientX, e.clientY,
            canvas.current.getBoundingClientRect(), info.width, info.height);
          if (coords) sendXpra(id, { action: "wheel", ...coords, deltaY: e.deltaY }); }}
        onBlur={() => sendXpra(id, { action: "blur" })}
        onKeyDown={e => { e.preventDefault(); if (!e.repeat) sendXpra(id, {
          action: "key", key: e.key, code: e.code, pressed: true,
          shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, altKey: e.altKey, metaKey: e.metaKey,
        }); }}
        onKeyUp={e => { e.preventDefault(); sendXpra(id, {
          action: "key", key: e.key, code: e.code, pressed: false,
          shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, altKey: e.altKey, metaKey: e.metaKey,
        }); }} />
      {!xAvailable && <div role="status" className="xwindow-notice">Xpra session disconnected; application preserved on server.</div>}
    </div>
    <footer className="xwindow-bottom">{info ? `${info.width} × ${info.height}` : "Waiting for window"} · Window is owned by the workspace Xpra session</footer>
  </section>;
}
