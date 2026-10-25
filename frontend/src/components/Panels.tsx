"use client";

import { motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import type { PanelId } from "@/lib/model";
import { PANEL_TITLES } from "@/lib/layout";
import { GraphWorkbench } from "./GraphWorkbench";
import { useWorkspace } from "./WorkspaceContext";

export function DockControls({ id }: { id: PanelId }) {
  const { dockPanel, floatPanel, graphFullscreen, setGraphFullscreen, closePanel } = useWorkspace();
  return <div className="dock-controls" aria-label={`${PANEL_TITLES[id]} positioning`}>
    <span className="subtle">Dock</span>
    <button onClick={() => dockPanel(id, "left")} title="Dock left" aria-label="Dock left">◧</button>
    <button onClick={() => dockPanel(id, "right")} title="Dock right" aria-label="Dock right">◨</button>
    <button onClick={() => dockPanel(id, "above")} title="Dock at top" aria-label="Dock at top">⬒</button>
    <button onClick={() => dockPanel(id, "below")} title="Dock at bottom" aria-label="Dock at bottom">⬓</button>
    <button onClick={() => floatPanel(id)} title="Float window" aria-label="Float window">▣</button>
    {id === "graph" && <button onClick={() => setGraphFullscreen(!graphFullscreen)}
      title={graphFullscreen ? "Restore graph" : "Maximize graph"} aria-label="Maximize graph">⛶</button>}
    {id !== "chat" && <button onClick={() => closePanel(id)} title="Minimize panel" aria-label="Minimize panel">✕</button>}
  </div>;
}

export function ChatPanel() {
  const { workspace, demo, entries, sendMessage, openPanel, chatBusy } = useWorkspace();
  const [draft, setDraft] = useState("");
  const anchor = useRef<HTMLDivElement>(null);
  useEffect(() => anchor.current?.scrollIntoView({ behavior: "smooth", block: "end" }), [entries.length]);
  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!draft.trim() || chatBusy) return;
    sendMessage(draft.trim());
    setDraft("");
  }
  return <section className="chat-surface" aria-label="Workspace conversation">
    <div className="chat-header">
      <span className="eyebrow">ACTIVE WORKSPACE</span>
      <h1>{workspace.name}</h1>
      <p>{workspace.summary}</p>
    </div>
    <div className="chat-stream" role="log" aria-label="Conversation messages" aria-live="polite">
      {entries.length === 0 && <div className="chat-welcome">
        <span className="welcome-symbol">✳</span>
        <h2>What are we working on?</h2>
        <p>Reasoning, tools, browser sessions and terminals will share this workspace.</p>
        <span className="subtle">{demo ? "Preview mode · no inference is connected" : "Live inference · chat history is not persisted by the backend yet"}</span>
      </div>}
      {entries.map(item => <div key={item.id} className={`chat-message ${item.role}`}>
        <span className="message-label">{item.role === "user" ? "You" : item.role === "assistant" ? "Potbot" : "Connection status"}</span>
        <p>{item.text}</p>
      </div>)}
      <div ref={anchor} />
    </div>
    <form className="composer" onSubmit={submit}>
      <div className="composer-tools">
        <motion.button layoutId="potbot-terminal-emergence" type="button" onClick={() => openPanel("terminal")}
          aria-label="Open workspace Bash terminal above composer" title="Open Bash terminal">⌘ <span>Terminal</span></motion.button>
        <button type="button" onClick={() => openPanel("browser")}
          aria-label="Open controlled browser pane" title="Open controlled browser">◎ <span>Browser</span></button>
        <span className="composer-hint">Shift + Enter for a newline</span>
      </div>
      <div className="composer-entry">
        <textarea value={draft} onChange={event => setDraft(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
          aria-label="Message Potbot" placeholder="Message Potbot, or steer the current reasoning…" rows={2} maxLength={16000} />
        <button className="send-button" type="submit" disabled={!draft.trim() || chatBusy} aria-label="Send message">{chatBusy ? "…" : "↑"}</button>
      </div>
      <span className="composer-disclaimer">{demo ? "DEMO • messages stay in this browser session" : chatBusy ? "POTBOT RESPONDING • requests may continue if connection drops" : "LIVE • workspace-scoped inference; conversation exists only in this tab"}</span>
    </form>
  </section>;
}

export function MenuPanel() {
  const { workspace, openPanel, isLive, logout, xWindows, openXWindow,
    xAvailable, xTransport } = useWorkspace();
  const sections: { heading: string; items: [PanelId, string][] }[] = [
    { heading: "WORKSPACE", items: [["graph", "Thought graph"], ["terminal", "Bash console"], ["browser", "Controlled browser"], ["applications", "Xpra applications"]] },
    { heading: "USER PREFERENCES", items: [["preferences", "General preferences"], ["downloads", "Downloads and transfers"], ["installations", "Model installations"]] },
  ];
  return <section className="menu-pane panel-scroll">
    <div className="menu-identity"><div className="mini-orb" style={{ borderColor: workspace.accent }}>{workspace.shortName}</div>
      <div><strong>{workspace.name}</strong><span>Workspace controls</span></div></div>
    {sections.map(section => <div key={section.heading} className="menu-section">
      <h2>{section.heading}</h2>
      {section.items.map(([id, label]) => <button className="menu-item" key={id} onClick={() => openPanel(id)}>
        <span>{label}</span><span aria-hidden="true">↗</span>
      </button>)}
    </div>)}
    {isLive && <div className="menu-section">
      <h2>XPRA APPLICATION WINDOWS</h2>
      {xAvailable && <p className="muted-note">{xTransport === "xpra-native-binary"
        ? "Xpra seamless · native binary stream · individual docked windows"
        : "Xpra disconnected"}</p>}
      {xWindows.map(window => <button key={window.id} className="menu-item"
        onClick={() => openXWindow(window.id)}><span>▣ {window.title}</span><span>↗</span></button>)}
      {!xAvailable && <p className="muted-note">Workspace Xpra bridge not configured or offline.</p>}
      {xAvailable && xWindows.length === 0 && <p className="muted-note">No X applications running yet.</p>}
    </div>}
    {isLive && <button className="menu-item" onClick={logout}><span>Sign out of operator workspace</span><span aria-hidden="true">⇥</span></button>}
    <p className="muted-note">Drag the titlebar to float this menu; dock it along any edge. The menu cannot maximize.</p>
    <DockControls id="menu" />
  </section>;
}

export function GraphPanel() {
  const { graphFullscreen, setGraphFullscreen } = useWorkspace();
  return <motion.section layoutId="potbot-graph-window" className="graph-pane">
    {graphFullscreen ? <div className="graph-fallback">Graph is open fullscreen.</div> : <GraphWorkbench />}
    <div className="graph-footer"><span>Drag to orbit · Scroll to zoom · Select a node</span>
      <DockControls id="graph" /></div>
    <button className="accessible-fullscreen" onClick={() => setGraphFullscreen(true)} aria-label="Maximize reasoning graph">⛶</button>
  </motion.section>;
}

export function FullscreenGraph() {
  const { setGraphFullscreen, closePanel } = useWorkspace();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = document.getElementById("potbot-maximized-graph");
    const focusable = () => Array.from(dialog?.querySelectorAll<HTMLElement>(
      "button:not([disabled]),input:not([disabled]),select:not([disabled])") ?? []);
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); setGraphFullscreen(false); }
      if (event.key !== "Tab") return;
      const all = focusable();
      if (!all.length) return;
      const first = all[0], last = all[all.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKey);
    document.getElementById("restore-graph")?.focus();
    return () => { document.removeEventListener("keydown", onKey); previous?.focus(); };
  }, [setGraphFullscreen]);
  return <div id="potbot-maximized-graph" className="graph-fullscreen" role="dialog" aria-modal="true" aria-label="Maximized reasoning graph">
    <div className="fullscreen-toolbar"><span>Thought graph — fullscreen</span>
      <button id="restore-graph" onClick={() => setGraphFullscreen(false)}>Restore pane ↙</button>
      <button onClick={() => { setGraphFullscreen(false); closePanel("graph"); }}>Minimize to orb ⊗</button>
    </div>
    <GraphWorkbench fullscreen />
  </div>;
}

export function TerminalPanel() {
  return <motion.div layoutId="potbot-terminal-emergence" className="placeholder-panel terminal-placeholder">
    <div className="terminal-welcome"><code>$ potbot console</code><p>Workspace Bash session not connected.</p>
      <small>UI-4 will connect this dockable frame to an authenticated workspace PTY. Closing the frame won't terminate the server session.</small></div>
    <DockControls id="terminal" />
  </motion.div>;
}

export function BrowserPanel() {
  return <div className="placeholder-panel panel-scroll">
    <div className="browser-address"><span>◎</span><span>No controlled session</span><span className="subtle">DISCONNECTED</span></div>
    <div className="empty-panel"><h2>Controlled browser</h2><p>UI-5 will connect a browser viewport stream here. Third-party sites cannot reliably be embedded in an iframe.</p></div>
    <DockControls id="browser" />
  </div>;
}

export function PreferencesPanel() {
  const { workspace, compact, setCompactPreference, graphView, updateGraphView, openPanel, demo } = useWorkspace();
  return <section className="settings-panel panel-scroll">
    <span className="eyebrow">USER PREFERENCES</span><h2>Workspace appearance</h2>
    <div className="settings-row"><div><strong>Current workspace</strong><small>{workspace.name}</small></div><span className="status-chip">ACTIVE</span></div>
    <label className="settings-row"><div><strong>Compact information density</strong>
      <small>Saved locally and remains active when preferences are closed.</small></div>
      <input type="checkbox" checked={compact} onChange={event => setCompactPreference(event.target.checked)} /></label>
    <label className="settings-row"><div><strong>Accessible graph list view</strong>
      <small>Switch off WebGL; graph filters and selection are preserved.</small></div>
      <input type="checkbox" checked={graphView.display === "list"}
        onChange={event => updateGraphView({ display: event.target.checked ? "list" : "3d" })} /></label>
    <label className="settings-row"><div><strong>Show graph diagnostics</strong>
      <small>Reports on-demand CPU render timing and estimated buffer size, not GPU FPS.</small></div>
      <input type="checkbox" checked={graphView.showDiagnostics}
        onChange={event => updateGraphView({ showDiagnostics: event.target.checked })} /></label>
    <h2>Activity windows</h2>
    <button type="button" className="menu-item" onClick={() => openPanel("downloads")}>Downloads and transfers <span>↗</span></button>
    <button type="button" className="menu-item" onClick={() => openPanel("installations")}>Model installations <span>↗</span></button>
    <p className="muted-note">{demo ? "Preview preferences are local to this browser." :
      "This operator session only supports local appearance settings. Account synchronization requires the backend preferences API."}
      {" "}Transfer and model-installation progress remain unavailable until the backend supplies authenticated telemetry.</p>
    <DockControls id="preferences" />
  </section>;
}

export function DownloadsPanel() {
  return <section className="settings-panel panel-scroll"><span className="eyebrow">USER PREFERENCES / TRANSFERS</span><h2>Downloads</h2>
    <div className="empty-panel"><p>No download progress feed is connected.</p><small>UI backend services must expose transfer IDs, bytes received, verified bytes, rate, retry state and cancellation rights.</small></div>
    <DockControls id="downloads" />
  </section>;
}

export function InstallationsPanel() {
  return <section className="settings-panel panel-scroll"><span className="eyebrow">USER PREFERENCES / MODELS</span><h2>Model installations</h2>
    <div className="empty-panel"><p>Installation service not connected.</p><small>Future backend reports quantization choice, disk/VRAM estimates, installation stages, checksums and model readiness.</small></div>
    <DockControls id="installations" />
  </section>;
}
