"use client";

import { motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import type { PanelId } from "@/lib/model";
import { bytesLabel, validateArtifact } from "@/lib/ui-resources";
import type { ArtifactManifest } from "@/lib/ui-resources";
import { PANEL_TITLES } from "@/lib/layout";
import { GraphWorkbench } from "./GraphWorkbench";
import { useWorkspace } from "./WorkspaceContext";
import { BrowserInspector } from "./BrowserInspector";

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
  const { workspace, demo, entries, sendMessage, openPanel, chatBusy,
    chatSessions, activeChatId, chatHistoryStatus, selectChatSession, newChatSession, reconcileChatSession } = useWorkspace();
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
      {!demo && <div className="conversation-manager">
        <label htmlFor="chat-session-picker">Conversation</label>
        <select id="chat-session-picker" value={activeChatId ?? ""}
          disabled={chatBusy} onChange={event => selectChatSession(event.target.value)}>
          {!activeChatId && <option value="">New conversation</option>}
          {chatSessions.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
        </select>
        <button type="button" disabled={chatBusy} onClick={newChatSession}>New</button>
        {entries.some(item => item.role === "status" && item.text.includes("pending")) &&
          <button type="button" disabled={chatBusy} onClick={reconcileChatSession}
            title="Acknowledge an abandoned request after the backend's six-minute safety window">Review pending</button>}
        <span role="status" className="subtle">{chatHistoryStatus}</span>
      </div>}
    </div>
    <div className="chat-stream" role="log" aria-label="Conversation messages" aria-live="polite">
      {entries.length === 0 && <div className="chat-welcome">
        <span className="welcome-symbol">✳</span>
        <h2>What are we working on?</h2>
        <p>Reasoning, tools, browser sessions and terminals will share this workspace.</p>
        <span className="subtle">{demo ? "Preview mode · no inference is connected" : "Live inference · completed turns are saved in this workspace"}</span>
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
      <span className="composer-disclaimer">{demo ? "DEMO • messages stay in this browser session" : chatBusy ? "POTBOT RESPONDING • requests may continue if connection drops" : "LIVE • workspace-scoped chat history is saved; interrupted turns need review"}</span>
    </form>
  </section>;
}

export function MenuPanel() {
  const { workspace, openPanel, isLive, logout, xWindows, openXWindow,
    xAvailable, xTransport } = useWorkspace();
  const sections: { heading: string; items: [PanelId, string][] }[] = [
    { heading: "WORKSPACE", items: [["graph", "Thought graph"], ["terminal", "Bash console"], ["browser", "Controlled browser"], ["applications", "Xpra applications"], ["activity", "Job activity"]] },
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
      <h2>OPERATOR SETUP</h2>
      <a className="menu-item" href="/setup/apis"><span>Remote API credentials</span><span aria-hidden="true">↗</span></a>
      <a className="menu-item" href="/setup/tools"><span>Tool testing and upgrades</span><span aria-hidden="true">↗</span></a>
    </div>}
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
  const { xAvailable, xWindows, openXWindow, openPanel, appInventory, launchApp, appStatus } = useWorkspace();
  const shells = xWindows.filter(item => /\b(?:xterm|terminal|bash|shell)\b/i.test(item.title)).slice(0, 20);
  return <motion.div layoutId="potbot-terminal-emergence" className="placeholder-panel terminal-placeholder">
    <div className="terminal-welcome">
      <code>$ potbot console</code>
      {(appInventory?.choices ?? []).filter(choice => choice.kind === "terminal").map(choice =>
        <button key={choice.kind} type="button" disabled={!xAvailable}
          onClick={() => launchApp(choice.kind)}>Launch sandbox {choice.title} ↗</button>)}
      <small role="status">{appStatus}</small>
      {xAvailable && shells.length ? <>
        <p>Workspace Xpra shell windows</p>
        {shells.map(item => <button type="button" key={item.id}
          onClick={() => openXWindow(item.id)}>Open {item.title} ↗</button>)}
      </> : <p>No workspace Bash session is connected.</p>}
      <small>Operator-approved X11 terminal sessions appear through Xpra and remain running when their Dockview pane closes. Direct PTY streaming from the composer is a separate capability.</small>
      <button type="button" onClick={() => openPanel("applications")}>Open Xpra application manager ↗</button>
    </div>
    <DockControls id="terminal" />
  </motion.div>;
}

export function BrowserPanel() {
  const { xWindows, xAvailable, openXWindow, openPanel, appInventory, launchApp, appStatus } = useWorkspace();
  return <div className="placeholder-panel panel-scroll">
    <div className="browser-address"><span>◎</span><span>Workspace Xpra application viewer</span>
      <span className="subtle">{xAvailable ? "CONNECTED" : "DISCONNECTED"}</span></div>
    <div className="empty-panel"><h2>Controlled browser</h2>
      <BrowserInspector />
      {(appInventory?.choices ?? []).filter(choice => choice.kind === "browser").map(choice =>
        <button key={choice.kind} type="button" disabled={!xAvailable}
          onClick={() => launchApp(choice.kind)}>Launch sandbox {choice.title} ↗</button>)}
      <small role="status">{appStatus}</small>
      <p>Browsers launched by the authorized workspace executor appear as individual Xpra windows, with keyboard, pointer, focus and docking routed through Xpra—not an insecure third-party iframe.</p>
      {xWindows.length > 0 ? <div className="applications-list" aria-label="Available Xpra windows">
        {xWindows.slice(0, 20).map(item => <button type="button" key={item.id}
          onClick={() => openXWindow(item.id)}>Open {item.title} ↗</button>)}
      </div> : <p>No Xpra application windows are currently reported.</p>}
      <button type="button" onClick={() => openPanel("applications")}>Manage all Xpra windows ↗</button>
      <small>Only operator-approved browser recipes can be launched. Browser navigation and keyboard/pointer controls use its Xpra window; network access remains governed by the workspace sandbox policy.</small>
    </div><DockControls id="browser" />
  </div>;
}

export function PreferencesPanel() {
  const { workspace, compact, setCompactPreference, graphView, updateGraphView, openPanel, demo, activityStatus, modelStatus, remotePreferences, preferencesStatus, refreshPreferences, updateUserPreferences } = useWorkspace();
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
    <h2>Backend connections</h2>
    <div className="settings-row"><div><strong>Workspace jobs</strong><small>{activityStatus}</small></div>
      <button type="button" onClick={() => openPanel("activity")}>Activity ↗</button></div>
    <div className="settings-row"><div><strong>Model catalogue</strong><small>{modelStatus}</small></div>
      <button type="button" onClick={() => openPanel("installations")}>Models ↗</button></div>
    <div className="settings-row"><div><strong>Remote API credentials</strong><small>Manage keys in the native vault.</small></div>
      <a href="/setup/apis">API setup ↗</a></div>
    <div className="settings-row"><div><strong>Shared tool upgrades</strong><small>Review measured S9 test evidence.</small></div>
      <a href="/setup/tools">Tool setup ↗</a></div>
    <h2>Activity windows</h2>
    <button type="button" className="menu-item" onClick={() => openPanel("downloads")}>Downloads and transfers <span>↗</span></button>
    <button type="button" className="menu-item" onClick={() => openPanel("installations")}>Model installations <span>↗</span></button>
    <h2>Saved workspace preferences</h2>
    <p role="status">{preferencesStatus}</p>
    <button type="button" onClick={refreshPreferences} disabled={demo}>Reload saved preferences</button>
    <label className="settings-row"><div><strong>Theme</strong><small>Saved for your principal and workspace.</small></div>
      <select aria-label="Workspace theme" disabled={!remotePreferences} value={remotePreferences?.settings.theme ?? "system"}
        onChange={e => updateUserPreferences({ theme: e.target.value as "system" | "dark" | "light" })}>
        <option value="system">System</option><option value="dark">Dark</option><option value="light">Light</option>
      </select></label>
    <label className="settings-row"><div><strong>Reduce motion</strong><small>Prefer fewer large panel transitions.</small></div>
      <input type="checkbox" disabled={!remotePreferences} checked={remotePreferences?.settings.reducedMotion ?? false}
        onChange={e => updateUserPreferences({ reducedMotion: e.target.checked })} /></label>
    <label className="settings-row"><div><strong>Download notifications</strong><small>Enable browser-side status notifications while the UI is open.</small></div>
      <input type="checkbox" disabled={!remotePreferences} checked={remotePreferences?.settings.downloadNotifications ?? false}
        onChange={e => updateUserPreferences({ downloadNotifications: e.target.checked })} /></label>
    <label className="settings-row"><div><strong>Xpra frame rate limit</strong><small>Preferred client budget; server may impose stricter limits.</small></div>
      <select aria-label="Xpra maximum frame rate" disabled={!remotePreferences}
        value={remotePreferences?.settings.xpraMaxFps ?? 30}
        onChange={e => updateUserPreferences({ xpraMaxFps: Number(e.target.value) as 10 | 15 | 24 | 30 | 60 })}>
        {[10, 15, 24, 30, 60].map(fps => <option key={fps} value={fps}>{fps} FPS</option>)}
      </select></label>
    <p className="muted-note">{demo ? "Preview preferences are local to this browser." :
      "Saved values are workspace-scoped in PostgreSQL. Window docking/layout coordinates remain browser-local."}</p>
    <DockControls id="preferences" />
  </section>;
}

export function DownloadsPanel() {
  const { demo, transfers, transfersStatus, refreshTransfers, cancelTransfer, openPanel } = useWorkspace();
  const active = transfers.filter(t => ["queued", "downloading", "verifying"].includes(t.state));
  const installed = transfers.filter(t => t.state === "installed");
  return <section className="settings-panel panel-scroll" aria-label="Verified model downloads">
    <span className="eyebrow">USER PREFERENCES / TRANSFERS</span><h2>Downloads</h2>
    <p role="status">{transfersStatus}</p>
    <button type="button" onClick={refreshTransfers} disabled={demo}>Refresh transfers</button>
    {!demo && <p className="muted-note">{active.length} active/queued · {installed.length} installed · only SHA-256-verified bytes count as installed.</p>}
    {transfers.length ? <ul className="activity-jobs">
      {transfers.map(job => <li className="activity-job" key={job.id}>
        <strong>{job.modelId}</strong>
        <small>{job.quantization} · {job.filename} · {job.state}{job.cancelRequested ? " · cancellation requested" : ""}</small>
        <progress max={job.expectedBytes} value={job.downloadedBytes} aria-label={`Download progress for ${job.modelId}`} />
        <span className="subtle">Downloaded {bytesLabel(job.downloadedBytes)} / {bytesLabel(job.expectedBytes)} · verified {bytesLabel(job.verifiedBytes)}</span>
        {job.errorCode && <small role="alert">{job.errorCode}</small>}
        {(["queued", "downloading", "verifying"].includes(job.state) && !job.cancelRequested) &&
          <button type="button" onClick={() => cancelTransfer(job.id)}>Request cancellation</button>}
      </li>)}
    </ul> : <div className="empty-panel"><p>{demo ? "Demo mode never downloads model files." : "No transfers have been recorded for this workspace."}</p>
      <button type="button" onClick={() => openPanel("installations")}>Browse model installations ↗</button></div>}
    <DockControls id="downloads" />
  </section>;
}

export function InstallationsPanel() {
  const { demo, models, modelStatus, refreshModels, transfers, transfersStatus,
    refreshTransfers, queueTransfer, openPanel } = useWorkspace();
  const [manifest, setManifest] = useState("{}");
  const [confirm, setConfirm] = useState(false);
  const [formStatus, setFormStatus] = useState("");
  const requested = (() => {
    try { return validateArtifact(JSON.parse(manifest)); } catch { return null; }
  })();
  function approve(event: React.FormEvent) {
    event.preventDefault();
    if (!requested || !confirm || demo) return;
    queueTransfer(requested);
    setFormStatus("Approved manifest submitted. Check Downloads for authoritative status.");
    setConfirm(false);
  }
  return <section className="settings-panel panel-scroll" aria-label="Model catalogue and installations">
    <span className="eyebrow">USER PREFERENCES / MODELS</span><h2>Model catalogue</h2>
    <p role="status">{modelStatus}</p>
    <button type="button" disabled={demo} onClick={() => refreshModels()}>Refresh catalogue</button>
    <p className="muted-note">These profiles describe candidate models, not installed weights or validated runtimes.
      Only the immutable artifact worker can mark an installation complete after checking file size and SHA-256.</p>
    <ul className="activity-jobs" aria-label="Available model catalogue profiles">
      {models.map(model => <li className="activity-job" key={model.id}>
        <strong>{model.id}</strong><small>{model.role} · {model.responseProtocol || "Protocol unspecified"}</small>
        <span className="subtle">{model.qualified ? "Qualified profile reported" : "Not qualified"} · {model.configured ? "Deployment configured" : "No deployment configured"}</span>
      </li>)}
    </ul>
    {models.length === 0 && <p className="muted-note">No model profiles reported by this workspace yet.</p>}
    <h2>Install a pinned quantized artifact</h2>
    <p role="status">{transfersStatus}</p>
    <p>Supply a trusted Hugging Face artifact manifest. For GGUF, its quantization must correspond to the exact filename and revision.
      The form cannot resolve arbitrary download URLs or guess checksums.</p>
    <form onSubmit={approve}>
      <label className="settings-row"><strong>Immutable artifact JSON</strong></label>
      <textarea aria-label="Pinned model artifact JSON" rows={8} spellCheck={false} value={manifest} onChange={e => { setManifest(e.target.value); setFormStatus(""); }} />
      <small>Required: model_id, artifact_repository, revision (40-hex commit), filename, quantization,
        sha256 (64-hex digest), size_bytes. No credentials or source URL parameters.</small>
      <label className="settings-row"><span>I explicitly approve this exact external download and storage use.</span>
        <input type="checkbox" checked={confirm} onChange={e => setConfirm(e.target.checked)} disabled={demo} /></label>
      <button type="submit" disabled={demo || !requested || !confirm}>Queue verified installation</button>
      {formStatus && <p role="status">{formStatus}</p>}
    </form>
    <div className="settings-row"><span>{transfers.filter(t => t.state === "installed").length} verified installations reported</span>
      <button type="button" onClick={() => { refreshTransfers(); openPanel("downloads"); }}>Open downloads ↗</button></div>
    <p className="muted-note">The dedicated transfer worker must be running to process queued files; downloading weights does not qualify a model for inference.</p>
    <DockControls id="installations" />
  </section>;
}
