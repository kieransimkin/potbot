"use client";

import dynamic from "next/dynamic";
import { motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import type { PanelId, GraphNode } from "@/lib/model";
import { DEMO_GRAPH } from "@/lib/model";
import { PANEL_TITLES } from "@/lib/layout";
import { useWorkspace } from "./WorkspaceContext";

const GraphScene = dynamic(() => import("./GraphScene"), {
  ssr: false, loading: () => <div className="graph-fallback">Loading GPU renderer…</div>,
});

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
  const { workspace, demo, entries, sendMessage, openPanel } = useWorkspace();
  const [draft, setDraft] = useState("");
  const anchor = useRef<HTMLDivElement>(null);
  useEffect(() => anchor.current?.scrollIntoView({ behavior: "smooth", block: "end" }), [entries.length]);
  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!draft.trim()) return;
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
        <span className="subtle">{demo ? "Preview mode · no inference is connected" : "Backend session API pending"}</span>
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
        <button className="send-button" type="submit" disabled={!draft.trim()} aria-label="Send message">↑</button>
      </div>
      <span className="composer-disclaimer">{demo ? "DEMO • messages stay in this browser session" : "No authenticated chat session has been configured"}</span>
    </form>
  </section>;
}

export function MenuPanel() {
  const { workspace, openPanel } = useWorkspace();
  const sections: { heading: string; items: [PanelId, string][] }[] = [
    { heading: "WORKSPACE", items: [["graph", "Thought graph"], ["terminal", "Bash console"], ["browser", "Controlled browser"]] },
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
    <p className="muted-note">Drag the titlebar to float this menu; dock it along any edge. The menu cannot maximize.</p>
    <DockControls id="menu" />
  </section>;
}

export function GraphPanel() {
  const { demo, graphFullscreen, setGraphFullscreen } = useWorkspace();
  const [selected, setSelected] = useState<GraphNode | null>(null);
  return <motion.section layoutId="potbot-graph-window" className="graph-pane">
    <div className="graph-content">
      {graphFullscreen ? <div className="graph-fallback">Graph is open in fullscreen.</div> : demo ? <GraphScene graph={DEMO_GRAPH} onInspect={setSelected} />
        : <div className="graph-unavailable"><strong>No active reasoning graph</strong><p>The backend graph projection and replay API is planned for UI-3.</p></div>}
      {demo && <span className="graph-demo-marker">SYNTHETIC GRAPH · DEMO</span>}
      {selected && <div className="graph-selection" role="status"><strong>{selected.label}</strong><small>{selected.kind} · {selected.state}</small>
        <button type="button" onClick={() => setSelected(null)} aria-label="Clear selected node">×</button></div>}
    </div>
    <div className="graph-footer"><span>{demo ? "Drag to orbit · Scroll to zoom · Select a node" : "Waiting for backend graph projection"}</span>
      <DockControls id="graph" /></div>
    <button className="accessible-fullscreen" onClick={() => setGraphFullscreen(true)} aria-label="Maximize reasoning graph">⛶</button>
  </motion.section>;
}

export function FullscreenGraph() {
  const { demo, setGraphFullscreen, closePanel } = useWorkspace();
  const [selected, setSelected] = useState<GraphNode | null>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    function escape(event: KeyboardEvent) { if (event.key === "Escape") setGraphFullscreen(false); }
    document.addEventListener("keydown", escape);
    document.getElementById("restore-graph")?.focus();
    return () => { document.removeEventListener("keydown", escape); previous?.focus(); };
  }, [setGraphFullscreen]);
  return <div className="graph-fullscreen" role="dialog" aria-modal="true" aria-label="Maximized reasoning graph">
    <div className="fullscreen-toolbar"><span>Thought graph — fullscreen</span>
      <button id="restore-graph" onClick={() => setGraphFullscreen(false)}>Restore pane ↙</button>
      <button onClick={() => { setGraphFullscreen(false); closePanel("graph"); }}>Minimize to orb ⊗</button>
    </div>
    {demo ? <GraphScene graph={DEMO_GRAPH} onInspect={setSelected} />
      : <div className="graph-unavailable">No graph has been loaded from Potbot.</div>}
    {selected && <div className="fullscreen-inspect"><b>{selected.label}</b><p>{selected.kind} · {selected.state}</p></div>}
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
  const { workspace } = useWorkspace();
  const [compact, setCompact] = useState(false);
  useEffect(() => {
    try { setCompact(localStorage.getItem(`potbot:ui:pref:compact:${workspace.id}`) === "1"); } catch {}
  }, [workspace.id]);
  useEffect(() => {
    document.body.classList.toggle("potbot-compact", compact);
    try { localStorage.setItem(`potbot:ui:pref:compact:${workspace.id}`, compact ? "1" : "0"); } catch {}
    return () => document.body.classList.remove("potbot-compact");
  }, [compact, workspace.id]);
  return <section className="settings-panel panel-scroll">
    <span className="eyebrow">USER PREFERENCES</span><h2>Workspace appearance</h2>
    <div className="settings-row"><div><strong>Current workspace</strong><small>{workspace.name}</small></div><span className="status-chip">ACTIVE</span></div>
    <label className="settings-row"><div><strong>Compact information density</strong><small>Saved locally for this workspace</small></div>
      <input type="checkbox" checked={compact} onChange={event => setCompact(event.target.checked)} /></label>
    <p className="muted-note">Account-level settings and model-provider preferences require the authenticated backend preferences API.</p>
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
