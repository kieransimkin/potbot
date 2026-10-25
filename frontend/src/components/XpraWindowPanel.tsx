"use client";

import { useEffect, useRef, useState } from "react";
import { attachXpraCanvas, markXpraInput, xpraFrameStats } from "@/lib/xpra-frames";
import type { XpraFrameStats } from "@/lib/xpra-frames";
import { normalizedPointerPosition, XWINDOW_ID } from "@/lib/xpra-contract";
import { useWorkspace } from "./WorkspaceContext";

/** Dockview chrome and one persistent canvas per Xpra *application*, not desktop. */
export function XpraWindowPanel({ params }: { params?: { windowId?: string } }) {
  const { xWindows, xWindowErrors, closeXWindow, xAvailable, sendXpra, appInventory } = useWorkspace();
  const xpraReadOnly = appInventory?.xpraReadOnly === true;
  const id = params?.windowId || "";
  const info = xWindows.find(x => x.id === id);
  const canvas = useRef<HTMLCanvasElement>(null);
  const lastPointer = useRef(0);
  const [stats, setStats] = useState<XpraFrameStats | null>(null);

  useEffect(() => {
    const surface = canvas.current;
    if (!surface || !XWINDOW_ID.test(id) || !info?.generation) return;
    const unregister = attachXpraCanvas(id, surface, info.generation);
    sendXpra(id, { action: "visibility", visible: true });
    sendXpra(id, { action: "frame" });
    let resizeTimer: ReturnType<typeof setTimeout> | undefined;
    let lastSize = "";
    const resize = new ResizeObserver(entries => {
      const target = entries[0]?.contentRect;
      if (!target) return;
      const width = Math.min(1920, Math.round(target.width));
      const height = Math.min(1200, Math.round(target.height));
      if (width < 65 || height < 65 || `${width}x${height}` === lastSize) return;
      lastSize = `${width}x${height}`;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => sendXpra(id, { action: "resize", width, height }), 120);
    });
    resize.observe(surface.parentElement || surface);
    // React wheel listeners can be passive. Install one explicit non-passive
    // listener to avoid scrolling the entire workspace when using X11 apps.
    const wheel = (event: WheelEvent) => {
      const bounds = surface.getBoundingClientRect();
      const point = normalizedPointerPosition(event.clientX, event.clientY,
        bounds, surface.width, surface.height);
      if (!point || xpraReadOnly) return;
      event.preventDefault();
      event.stopPropagation();
      markXpraInput(id);
      sendXpra(id, { action: "wheel", ...point,
        deltaY: Math.max(-1000, Math.min(1000, event.deltaY)) });
    };
    surface.addEventListener("wheel", wheel, { passive: false });
    const meter = setInterval(() => setStats(xpraFrameStats(id)), 1000);
    return () => {
      resize.disconnect();
      clearTimeout(resizeTimer);
      clearInterval(meter);
      surface.removeEventListener("wheel", wheel);
      sendXpra(id, { action: "visibility", visible: false });
      sendXpra(id, { action: "blur" });
      unregister();
    };
  }, [id, info?.generation, sendXpra, xpraReadOnly]);

  function pointer(event: React.PointerEvent<HTMLCanvasElement>, action: "pointer" | "button", pressed = false) {
    const surface = canvas.current;
    if (!surface || !info || xpraReadOnly) return;
    if (action === "pointer" && performance.now() - lastPointer.current < 16) return;
    lastPointer.current = performance.now();
    const point = normalizedPointerPosition(event.clientX, event.clientY,
      surface.getBoundingClientRect(), surface.width, surface.height);
    if (!point) return;
    if (action === "pointer") sendXpra(id, { action, ...point });
    else {
      markXpraInput(id);
      sendXpra(id, { action, ...point, button: event.button, pressed });
      if (pressed) {
        event.currentTarget.setPointerCapture(event.pointerId);
        event.currentTarget.focus();
        sendXpra(id, { action: "focus" });
      }
    }
  }
  return <section className="xwindow-panel" data-xpra-window-id={id}
    aria-label={`Xpra application ${info?.title || id}`}>
    <header className="xwindow-toolbar">
      <strong title={info?.title}>{info?.title || "Xpra window"}</strong>
      <small>Native Xpra damage stream · Dockview chrome</small>
      <button type="button" onClick={() => closeXWindow(id)} aria-label="Minimize application pane">Minimize ↗</button>
    </header>
    {xpraReadOnly && <p role="status">Read-only Xpra display: direct keyboard and mouse input is disabled. Use the reserved browser inspector controls.</p>}
    <div className="xwindow-viewport">
      <canvas ref={canvas} width={640} height={480}
        className="xpra-app-canvas" aria-label={`Interactive Xpra window: ${info?.title || id}`}
        role="img" tabIndex={0}
        onFocus={() => { if (!xpraReadOnly) sendXpra(id, { action: "focus" }); }}
        onPointerDown={e => pointer(e, "button", true)}
        onPointerUp={e => pointer(e, "button", false)}
        onPointerMove={e => pointer(e, "pointer")}
        onPointerCancel={e => { if (e.currentTarget.hasPointerCapture(e.pointerId))
          e.currentTarget.releasePointerCapture(e.pointerId); sendXpra(id, { action: "blur" }); }}
        onContextMenu={e => e.preventDefault()}
        onBlur={() => sendXpra(id, { action: "blur" })}
        onKeyDown={e => { if (xpraReadOnly) return; e.preventDefault(); e.stopPropagation(); if (!e.repeat) {
          markXpraInput(id); sendXpra(id, {
          action: "key", key: e.key, code: e.code, pressed: true,
          shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, altKey: e.altKey, metaKey: e.metaKey,
        }); } }}
        onKeyUp={e => { if (xpraReadOnly) return; e.preventDefault(); e.stopPropagation(); sendXpra(id, {
          action: "key", key: e.key, code: e.code, pressed: false,
          shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, altKey: e.altKey, metaKey: e.metaKey,
        }); }} />
      {!xAvailable && <div role="status" className="xwindow-notice">Xpra disconnected; the application remains on the workspace server.</div>}
      {xWindowErrors[id] && <div role="alert" className="xwindow-error">
        <span>{xWindowErrors[id]}</span>
        <button type="button" onClick={() => sendXpra(id, { action: "frame" })}>Retry frame</button>
      </div>}
    </div>
    <footer className="xwindow-bottom">
      <span>{info ? `${info.width} × ${info.height}` : "Awaiting Xpra window"} · workspace-owned application</span>
      <span aria-label="Xpra frame diagnostics">{stats ? `${stats.fps} FPS · ${stats.incremental} partial · ${stats.estimatedDecodedPixelBytes / 1024 / 1024 < 100 ? (stats.estimatedDecodedPixelBytes / 1024 / 1024).toFixed(1) : Math.round(stats.estimatedDecodedPixelBytes / 1024 / 1024)} MiB copied` : "Awaiting first frame"}</span>
      <span title="Browser-side measurements; the next frame may be unrelated to an input">
        {stats?.p95FrameIntervalMs !== null && stats?.p95FrameIntervalMs !== undefined
          ? `P95 frame gap ${stats.p95FrameIntervalMs} ms` : "Frame timing pending"}
        {stats?.medianInputToNextFrameMs !== null && stats?.medianInputToNextFrameMs !== undefined
          ? ` · Input→frame ${stats.medianInputToNextFrameMs} ms (proxy, n=${stats.inputSamples})` : ""}
      </span>
    </footer>
  </section>;
}
