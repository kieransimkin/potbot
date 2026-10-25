"use client";

import { useMemo, useState } from "react";
import { projectXpraWindows } from "@/lib/xpra-window-manager";
import type { WindowFilter } from "@/lib/xpra-window-manager";
import { useWorkspace } from "./WorkspaceContext";
import { DockControls } from "./Panels";

/** A dockable switcher for every Xpra application, including minimized panes. */
export function XpraApplicationsPanel() {
  const { xAvailable, xWindows, xMinimizedWindows, xTransport, xReconnectAttempts,
    retryXpra, openXWindow, closeXWindow } = useWorkspace();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<WindowFilter>("all");
  const display = useMemo(() => projectXpraWindows(xWindows, xMinimizedWindows, query, filter),
    [xWindows, xMinimizedWindows, query, filter]);
  return <section className="applications-pane panel-scroll" aria-label="Workspace Xpra applications">
    <header className="applications-header">
      <span className="eyebrow">WORKSPACE X11</span>
      <h2>Application windows</h2>
      <p>{display.total} windows · {display.minimized} minimized · {xTransport ?? "not connected"}</p>
      <p className={xAvailable ? "connection-ok" : "connection-warning"} role="status">
        {xAvailable ? "Xpra connected" : "Xpra disconnected — applications may still be running"}
        {!xAvailable && <> · {xReconnectAttempts} reconnect attempt(s)</>}
      </p>
      {!xAvailable && <button type="button" onClick={retryXpra}>Reconnect Xpra</button>}
    </header>
    <div className="applications-search">
      <label htmlFor="xpra-app-search">Find window</label>
      <input id="xpra-app-search" type="search" value={query}
        onChange={event => setQuery(event.target.value)} maxLength={120}
        placeholder="Application name or X window ID" />
      <div className="applications-filters" role="group" aria-label="Window visibility">
        {(["all", "visible", "minimized"] as const).map(value => <button
          key={value} type="button" aria-pressed={filter === value}
          onClick={() => setFilter(value)}>{value === "all" ? "All" : value === "visible" ? "Visible" : "Minimized"}</button>)}
      </div>
    </div>
    <ul className="applications-list" aria-label="Xpra application windows">
      {display.items.map(window => <li key={`${window.id}-${window.generation}`} className="applications-item">
        <span className="applications-symbol" aria-hidden="true">▣</span>
        <span className="applications-description">
          <strong title={window.title}>{window.title || "Untitled X window"}</strong>
          <small>{window.id} · {window.width} × {window.height} · {window.minimized ? "Minimized" : "Visible"}</small>
        </span>
        <button type="button" onClick={() => openXWindow(window.id)}
          aria-label={`${window.minimized ? "Restore" : "Focus"} ${window.title}`}>
          {window.minimized ? "Restore" : "Focus"}
        </button>
        {!window.minimized && <button type="button" onClick={() => closeXWindow(window.id)}
          aria-label={`Minimize ${window.title}`}>−</button>}
      </li>)}
    </ul>
    {display.limited && <p role="status">Showing 256 windows. Refine the search for more.</p>}
    {!display.items.length && <p className="muted-note">{query ? "No windows match that search." : "No matching X applications."}</p>}
    <p className="muted-note">Closing or minimizing a pane leaves the X11 application running in this workspace. Only the workspace executor may terminate its process.</p>
    <DockControls id="applications" />
  </section>;
}
