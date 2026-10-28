"use client";

import { useMemo, useState } from "react";
import { projectXpraWindows } from "@/lib/xpra-window-manager";
import type { WindowFilter } from "@/lib/xpra-window-manager";
import { useWorkspace } from "./WorkspaceContext";
import { DockControls } from "./Panels";
import { AgentXpraApplications } from "./AgentXpraStreams";

/** A dockable switcher for every Xpra application, including minimized panes. */
export function XpraApplicationsPanel() {
  const { xAvailable, xWindows, xMinimizedWindows, xTransport, xReconnectAttempts,
    retryXpra, openXWindow, closeXWindow, appInventory, appStatus, refreshApps, launchApp, appAction } = useWorkspace();
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
    <div className="applications-launcher" aria-label="Operator-approved application sessions">
      <h3>Launch sandbox application</h3>
      <p role="status">{appStatus}</p>
      <div className="applications-launch-buttons">
        {(appInventory?.choices ?? []).map(choice => <button type="button" key={choice.kind}
          disabled={!xAvailable || !appInventory?.configured}
          onClick={() => launchApp(choice.kind)} title={`Start ${choice.title} inside the qualified workspace`}>
          <span aria-hidden="true">＋</span> {choice.title}
        </button>)}
        <button type="button" onClick={refreshApps}>Refresh sessions</button>
      </div>
      {(!appInventory?.configured || !appInventory.choices.length) &&
        <p className="muted-note">No operator-approved launch recipes are available. Xpra windows can still be viewed and controlled.</p>}
      {(appInventory?.sessions ?? []).length > 0 && <ul className="applications-list" aria-label="Workspace application processes">
        {appInventory!.sessions.map(app => <li key={app.id} className="applications-item">
          <span className="applications-symbol" aria-hidden="true">◈</span>
          <span className="applications-description"><strong>{app.title}</strong>
            <small>{app.state}{app.detached ? " · detached" : " · attached"} · {app.id.slice(0, 8)}</small>
          </span>
          {app.state === "running" && <>
            <button type="button" onClick={() => appAction(app.id, app.detached ? "attach" : "detach")}>
              {app.detached ? "Reattach" : "Detach"}
            </button>
            <button type="button" onClick={() => appAction(app.id, "terminate")}
              aria-label={`Terminate ${app.title}`}>Stop</button>
          </>}
        </li>)}
      </ul>}
    </div>
    <AgentXpraApplications />
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
    <p className="muted-note">Closing or minimizing a pane leaves the X11 application running in this workspace. Termination is an explicit, operator-authorized action above; closing a pane alone does not stop the process.</p>
    <DockControls id="applications" />
  </section>;
}
