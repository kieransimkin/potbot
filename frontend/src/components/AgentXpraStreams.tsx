"use client";

import { createContext, useContext, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { MutableRefObject } from "react";
import type { DockviewApi } from "dockview-react";
import { AgentXpraHub } from "@/lib/agent-xpra-hub";
import type { AgentXpraSession } from "@/lib/agent-xpra-contract";
import { agentXpraSessions, parseAgentWindowKey } from "@/lib/agent-xpra-contract";
import { validXpraWindow, xpraFrameURL } from "@/lib/xpra-contract";

const Context = createContext<AgentXpraHub | null>(null);
export const AgentXpraProvider = Context.Provider;
export function useAgentXpra(): AgentXpraHub {
  const value = useContext(Context);
  if (!value) throw Error("Agent Xpra pane is outside an operator workspace");
  return value;
}

function PrivateStream({session, hub, workspaceId}: {session: AgentXpraSession;
  hub: AgentXpraHub; workspaceId: string}) {
  const iframe = useRef<HTMLIFrameElement>(null);
  const [src, setSrc] = useState<string | null>(null);
  const channel = useRef("");

  useEffect(() => {
    let alive = true, retry: ReturnType<typeof setTimeout> | undefined;
    let readyWatchdog: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0, epoch = 0, connecting = false;
    const schedule = () => {
      if (!alive) return;
      hub.disconnected(session.id, epoch || undefined);
      epoch = 0;
      setSrc(null);
      clearTimeout(readyWatchdog);
      clearTimeout(retry);
      attempts = Math.min(attempts + 1, 6);
      retry = setTimeout(() => void connect(), Math.min(30000, 700 * (2 ** attempts)));
    };
    const connect = async () => {
      if (!alive || connecting) return;
      connecting = true;
      try {
        const response = await fetch(`/api/ui/xpra/agent-sessions/${session.agentId}/${session.id}/ticket`, {
          method:"POST", credentials:"same-origin", cache:"no-store",
          headers:{"x-potbot-workspace": workspaceId},
        });
        if (!response.ok) throw Error("Ticket unavailable");
        const data = await response.json() as {workspaceId?: string; agentId?: string;
          sessionId?: string; wsUrl?: string; viewOnly?: boolean; expiresInSeconds?: number};
        if (!alive || data.workspaceId !== workspaceId || data.agentId !== session.agentId ||
            data.sessionId !== session.id || data.viewOnly !== true || data.expiresInSeconds !== 30 ||
            typeof data.wsUrl !== "string") throw Error("Untrusted ticket scope");
        channel.current = Array.from(crypto.getRandomValues(new Uint8Array(16)))
          .map(b => b.toString(16).padStart(2, "0")).join("");
        setSrc(xpraFrameURL(data.wsUrl, channel.current));
        // A stalled handshake must not leave a phantom live stream forever.
        clearTimeout(readyWatchdog);
        readyWatchdog = setTimeout(() => { if (alive && epoch === 0) schedule(); }, 12000);
      } catch { schedule(); }
      finally { connecting = false; }
    };
    const message = (event: MessageEvent) => {
      if (!alive || event.origin !== location.origin || event.source !== iframe.current?.contentWindow) return;
      const data = event.data as Record<string, unknown> | null;
      if (!data || data.type !== "potbot:xpra") return;
      if (data.channel !== channel.current) {
        // The former decoder can race a reconnect; discard its transferred bitmap.
        if (data.action === "frame" && typeof ImageBitmap !== "undefined" &&
            data.bitmap instanceof ImageBitmap) data.bitmap.close();
        return;
      }
      if (data.action === "ready") {
        clearTimeout(readyWatchdog);
        attempts = 0;
        // View-only agent streams are deliberately bandwidth-capped separately
        // from the interactive operator workspace connection.
        iframe.current?.contentWindow?.postMessage({type:"potbot:xpra-control",
          channel:channel.current, action:"set-fps", fps:10}, location.origin);
        epoch = hub.attachedStream(session.id, (id, action, visible) => {
          if (event.source !== iframe.current?.contentWindow || !iframe.current?.contentWindow) return;
          iframe.current.contentWindow.postMessage({type:"potbot:xpra-control", channel:channel.current,
            id, action, ...(action === "visibility" ? {visible} : {})}, location.origin);
        });
      } else if (data.action === "disconnected") schedule();
      else if (data.action === "open" && epoch && validXpraWindow(data)) hub.observe(session, data, epoch);
      else if (data.action === "close" && epoch && typeof data.id === "string" &&
          typeof data.generation === "number") {
        try { hub.closed(session.id, data.id, data.generation, epoch); } catch { /* Invalid XID */ }
      } else if (data.action === "frame" && typeof ImageBitmap !== "undefined" &&
          data.bitmap instanceof ImageBitmap) {
        if (epoch) hub.deliver(session.id, data, data.bitmap, epoch);
        else data.bitmap.close();
      }
    };
    window.addEventListener("message", message);
    void connect();
    return () => {
      alive = false; clearTimeout(retry); clearTimeout(readyWatchdog);
      window.removeEventListener("message", message);
      hub.disconnected(session.id, epoch || undefined);
    };
  }, [workspaceId, session.id, session.agentId, hub]);

  return src ? <iframe ref={iframe} src={src} title={`Read-only Xpra decoder for agent ${session.agentId.slice(0,8)}`}
    className="xpra-protocol-frame" aria-hidden="true" tabIndex={-1}
    sandbox="allow-scripts allow-same-origin"/> : null;
}

/** Every agent uses an independent native Xpra HTML5 decoder and 1-use ticket. */
export function AgentXpraStreamHost({workspaceId, enabled, apiRef}: {workspaceId: string;
  enabled: boolean; apiRef: MutableRefObject<DockviewApi | null>}) {
  const hub = useAgentXpra();
  const [sessions, setSessions] = useState<AgentXpraSession[]>([]);
  useEffect(() => {
    hub.setDock(() => apiRef.current);
    // Never render the previous workspace's agent list during an auth/scope change.
    setSessions([]);
    hub.setSessions([]);
    if (!enabled) { hub.reset(); return; }
    let live = true;
    const fetchInventory = async () => {
      try {
        const response = await fetch("/api/ui/xpra/agent-sessions", {method:"GET", cache:"no-store",
          credentials:"same-origin",headers:{"x-potbot-workspace":workspaceId}});
        if (!response.ok) throw Error("No private display inventory");
        const document = await response.json();
        const result = agentXpraSessions(document, workspaceId);
        if (result === null) throw Error("Invalid display inventory");
        if (!live) return;
        setSessions(result);
        hub.setSessions(result);
        hub.setStatus(`${result.length} read-only agent browser${result.length === 1 ? "" : "s"}`);
      } catch {
        if (live) {
          hub.setStatus("Agent display service unavailable");
          setSessions([]); hub.setSessions([]);
        }
      }
    };
    void fetchInventory();
    const poll = setInterval(() => void fetchInventory(), 7000);
    return () => { live = false; clearInterval(poll); hub.reset(); };
  }, [workspaceId, enabled, hub, apiRef]);
  return <>{sessions.map(session => <PrivateStream key={`${workspaceId}:${session.id}`}
    workspaceId={workspaceId} session={session} hub={hub}/>)}</>;
}

/** Native Xpra window rendered as an independent Dockview pane, view only. */
export function AgentXpraWindowPanel({params}: {params?: {agentWindowKey?: string}}) {
  const hub = useAgentXpra();
  const snapshot = useSyncExternalStore(hub.subscribe, hub.snapshot, hub.snapshot);
  const key = params?.agentWindowKey ?? "";
  const parsed = parseAgentWindowKey(key);
  const windowInfo = snapshot.windows.find(w => w.key === key);
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (!windowInfo || !parsed || parsed.workspaceId !== hub.workspaceId || !canvas.current) return;
    return hub.attachCanvas(key, canvas.current);
  }, [hub, key, windowInfo?.epoch, windowInfo?.generation]);
  return <section className="xwindow-panel" aria-label="Read-only private agent Xpra window">
    <header className="xwindow-toolbar"><strong>{windowInfo?.title ?? "Agent browser"}</strong>
      <small>Agent {windowInfo?.agentId.slice(0,8) ?? "—"} · Private Xpra · View only</small></header>
    <div className="xwindow-viewport">
      <canvas ref={canvas} width={640} height={480} role="img"
        className="xpra-app-canvas" aria-label={`Read-only live agent browser: ${windowInfo?.title ?? "Disconnected"}`}/>
      {!windowInfo?.connected && <p className="xwindow-notice" role="status">Agent display disconnected or reconnecting</p>}
    </div>
    <footer className="xwindow-bottom">Keyboard/mouse input is disabled. Agent actions use the reserved browser Cog interface.</footer>
  </section>;
}

export function AgentXpraApplications() {
  const hub = useAgentXpra();
  const snapshot = useSyncExternalStore(hub.subscribe, hub.snapshot, hub.snapshot);
  return <section className="applications-launcher" aria-label="Private agent Xpra browser displays">
    <h3>Agent-private browser displays</h3>
    <p role="status">{snapshot.status}</p>
    {snapshot.sessions.map(session => <div key={session.id} className="applications-item">
      <strong>Agent {session.agentId.slice(0,8)}</strong>
      <span>{session.title} · {snapshot.windows.filter(w => w.sessionId === session.id).length} window(s)</span>
      <span>View only</span>
    </div>)}
    {snapshot.windows.map(w => <div className="applications-item" key={w.key}>
      <span>{w.title} · {w.width}×{w.height} · {w.connected ? "Live" : "Reconnecting"}</span>
      <button type="button" onClick={() => hub.openWindow(w.key)}>Open agent window</button>
    </div>)}
    <p className="muted-note">Each agent uses a distinct Xpra display, decoder, and stream ticket. No agent reservation token is sent to the viewer.</p>
  </section>;
}
