"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { DockviewApi, DockviewReadyEvent } from "dockview-react";
import { DockviewReact } from "dockview-react";
import { AnimatePresence, LayoutGroup, motion } from "motion/react";
import { bootWorkspaces } from "@/lib/boot";
import { readChatStream } from "@/lib/chat-stream";
import { DEFAULT_GRAPH_VIEW, mergeGraphTimeline, sanitizedGraphView } from "@/lib/graph-workbench";
import type { GraphView, Timeline, GraphEvent } from "@/lib/graph-workbench";
import { RUN_ID } from "@/lib/live-schema";
import { validXpraWindow, xpraFrameURL, validXpraControl, windowPanelId, XWINDOW_ID } from "@/lib/xpra-contract";
import { deliverXpraFrame } from "@/lib/xpra-frames";
import type { XWindowInfo, XWindowTransport } from "@/lib/xpra-contract";
import { decodeSavedLayout, layoutStorageKey, PANEL_TITLES, shouldMinimizePanel } from "@/lib/layout";
import type { ChatEntry, DockEdge, PanelId, Workspace, GraphProjection } from "@/lib/model";
import { WorkspaceOrbit } from "./WorkspaceOrbit";
import { WorkspaceProvider } from "./WorkspaceContext";
import { XpraWindowPanel } from "./XpraWindowPanel";
import { BrowserPanel, ChatPanel, DownloadsPanel, FullscreenGraph, GraphPanel, InstallationsPanel, MenuPanel, PreferencesPanel, TerminalPanel } from "./Panels";

const COMPONENTS = {
  chat: ChatPanel, graph: GraphPanel, menu: MenuPanel, terminal: TerminalPanel,
  browser: BrowserPanel, preferences: PreferencesPanel, downloads: DownloadsPanel,
  installations: InstallationsPanel, xwindow: XpraWindowPanel,
};

function floatingDefaults(panel: PanelId) {
  switch (panel) {
    case "graph": return { position: { top: 90, right: 35 }, width: 510, height: 385 };
    case "menu": return { position: { top: 112, left: 18 }, width: 268, height: 440 };
    case "terminal": return { position: { bottom: 90, left: 90 }, width: 590, height: 310 };
    case "browser": return { position: { top: 110, right: 65 }, width: 650, height: 460 };
    default: return { position: { top: 145, left: 105 }, width: 425, height: 400 };
  }
}

function initialPanel(api: DockviewApi, id: PanelId) {
  if (api.getPanel(id)) return;
  if (id === "chat") {
    api.addPanel({ id, component: id, title: PANEL_TITLES[id], minimumWidth: 300, minimumHeight: 240 });
  } else {
    const floating = floatingDefaults(id);
    // Keep the first frame on-screen on small devices; Dockview handles later moves.
    const width = Math.min(floating.width, Math.max(260, window.innerWidth - 40));
    const height = Math.min(floating.height, Math.max(190, window.innerHeight - 170));
    api.addPanel({ id, component: id, title: PANEL_TITLES[id],
      floating: { ...floating, width, height },
      minimumWidth: id === "menu" ? 205 : 255, minimumHeight: 180 });
  }
}

function DockedScene({ workspace, onGraphMinimized, onXWindowHidden }: {
  workspace: Workspace;
  onGraphMinimized: (value: boolean) => void;
  onXWindowHidden: (windowId: string) => void;
}) {
  const apiRef = useDockApi();
  useEffect(() => () => { apiRef.current = null; }, [apiRef]);
  const onReady = useCallback(({ api }: DockviewReadyEvent) => {
    apiRef.current = api;
    let restored = false;
    try {
      const saved = decodeSavedLayout(localStorage.getItem(layoutStorageKey(workspace.id)), workspace.id);
      if (saved) {
        api.fromJSON(saved.dock as Parameters<DockviewApi["fromJSON"]>[0]);
        restored = !!api.getPanel("chat");
      }
    } catch { /* Corrupt or older saved layout: use a safe default. */ }
    if (!restored) {
      // fromJSON may have partially restored a layout; don't leave ghost groups.
      api.clear();
      initialPanel(api, "chat");
      initialPanel(api, "graph");
      initialPanel(api, "menu");
    }
    const persist = () => {
      try {
        localStorage.setItem(layoutStorageKey(workspace.id), JSON.stringify({
          version: 1, workspaceId: workspace.id, dock: api.toJSON(),
          graphMinimized: !api.getPanel("graph"), savedAt: Date.now(),
        }));
      } catch { /* Private mode/storage unavailable: layout stays in memory. */ }
    };
    let debounce: ReturnType<typeof setTimeout> | undefined;
    const changed = api.onDidLayoutChange(() => {
      onGraphMinimized(!api.getPanel("graph"));
      clearTimeout(debounce);
      debounce = setTimeout(persist, 350);
    });
    const removed = api.onDidRemovePanel(panel => {
      // Dockview sends remove+add when relocating. Check after the transaction.
      queueMicrotask(() => {
        if (apiRef.current !== api || api.getPanel(panel.id)) return;
        if (panel.id === "chat") initialPanel(api, "chat"); // chat cannot be permanently closed
        if (panel.id === "graph") onGraphMinimized(true);
        if (panel.id.startsWith("x11:") && XWINDOW_ID.test(panel.id.slice(4)))
          onXWindowHidden(panel.id.slice(4));
      });
    });
    const maximized = api.onDidMaximizedGroupChange(() => {
      // Menu and conversation must not occupy the entire scene.
      if (api.getPanel("menu")?.api.isMaximized() || api.getPanel("chat")?.api.isMaximized()) api.exitMaximizedGroup();
    });
    onGraphMinimized(!api.getPanel("graph"));
    // Cleanup is handled when workspace-specific Dockview unmounts.
    cleanupRef.current = () => { clearTimeout(debounce); persist(); changed.dispose(); removed.dispose(); maximized.dispose(); };
  }, [apiRef, workspace.id, onGraphMinimized, onXWindowHidden]);
  const cleanupRef = useRef<() => void>(() => {});
  useEffect(() => () => cleanupRef.current(), []);
  return <DockviewReact className="dockview-theme-dark potbot-dockview" onReady={onReady}
    components={COMPONENTS} floatingGroupBounds="boundedWithinViewport"
    floatingGroupDragHandle="titlebar"
    keyboardNavigation />;
}

// Shared across panel commands and this workspace's Dockview instance.
const DockApiContext = createContext<React.MutableRefObject<DockviewApi | null> | null>(null);
function useDockApi() {
  const ref = useContext(DockApiContext);
  if (!ref) throw new Error("Docking panel is outside the workspace layout");
  return ref;
}

type LoginState = "checking" | "login" | "disconnected" | "ready";

export function OrbWorkspace({ demo }: { demo: boolean }) {
  const initial = useMemo(() => bootWorkspaces(demo), [demo]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>(initial.workspaces);
  const [gateway, setGateway] = useState<LoginState>(demo ? "ready" : "checking");
  const [notice, setNotice] = useState("");
  const [password, setPassword] = useState("");
  const [selected, setSelected] = useState(0);
  const [messages, setMessages] = useState<Record<string, ChatEntry[]>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [runIds, setRunIds] = useState<Record<string, string>>({});
  const [graphs, setGraphs] = useState<Record<string, GraphProjection>>({});
  const [xWindowsByWorkspace, setXWindowsByWorkspace] = useState<Record<string, XWindowInfo[]>>({});
  const [xAvailable, setXAvailable] = useState(false);
  const [xpraIframeSrc, setXpraIframeSrc] = useState<string | null>(null);
  const xpraIframe = useRef<HTMLIFrameElement | null>(null);
  const xpraChannel = useRef("");
  const [xTransportByWorkspace, setXTransportByWorkspace] = useState<Record<string, XWindowTransport | null>>({});
  const xSeen = useRef<Record<string, Set<string>>>({});
  const xHidden = useRef<Record<string, Set<string>>>({});
  const [graphHealth, setGraphHealth] = useState<Record<string, string>>({});
  const [graphTimelines, setGraphTimelines] = useState<Record<string, Timeline>>({});
  const [graphViews, setGraphViews] = useState<Record<string, GraphView>>({});
  const [compactPrefs, setCompactPrefs] = useState<Record<string, boolean>>({});
  const cursors = useRef<Record<string, number>>({});
  const requests = useRef<Map<string, AbortController>>(new Map());
  const [graphMinimized, setGraphMinimized] = useState(false);
  const [graphFullscreen, setGraphFullscreen] = useState(false);
  const apiRef = useRef<DockviewApi | null>(null);
  const workspace = workspaces[selected];

  const loadLive = useCallback(async () => {
    try {
      const response = await fetch("/api/ui/bootstrap", { cache: "no-store", credentials: "same-origin" });
      if (response.status === 401) { setGateway("login"); return; }
      if (!response.ok) throw new Error("Potbot backend is unavailable or not configured.");
      const data: unknown = await response.json();
      const doc = data as { workspaces?: Workspace[] };
      if (!Array.isArray(doc.workspaces) || !doc.workspaces.length) throw new Error("No authorized workspace was returned.");
      // Backend-for-frontend returns only a scoped and whitelisted workspace.
      setWorkspaces(doc.workspaces);
      setSelected(0);
      setNotice("");
      setGateway("ready");
    } catch { setGateway("disconnected"); setNotice("Cannot connect to the authorized Potbot workspace. Check backend configuration and service health."); }
  }, []);

  useEffect(() => {
    if (demo) return;
    let active = true;
    void fetch("/api/ui/auth/status", { cache: "no-store" }).then(r => r.json()).then((data: { configured?: boolean; authenticated?: boolean }) => {
      if (!active) return;
      if (!data.configured) { setGateway("disconnected"); setNotice("Operator authentication is not configured on this Next.js server."); }
      else if (!data.authenticated) setGateway("login");
      else void loadLive();
    }).catch(() => { if (active) { setGateway("disconnected"); setNotice("Operator authentication service is unavailable."); } });
    return () => { active = false; };
  }, [demo, loadLive]);

  useEffect(() => () => {
    for (const controller of requests.current.values()) controller.abort();
    requests.current.clear();
  }, []);

  useEffect(() => {
    try {
      const last = localStorage.getItem("potbot:ui:selected-workspace:v1");
      const index = workspaces.findIndex(item => item.id === last);
      if (index >= 0) setSelected(index);
    } catch { /* Storage is optional. */ }
  }, [workspaces]);

  // View preferences are local UI state only; never sent to the reasoning backend.
  useEffect(() => {
    if (!workspace) return;
    const id = workspace.id;
    try {
      const stored = localStorage.getItem(`potbot:ui:graph-view:v1:${id}`);
      setGraphViews(state => state[id] ? state : { ...state,
        [id]: sanitizedGraphView(stored ? JSON.parse(stored) : DEFAULT_GRAPH_VIEW) });
    } catch { setGraphViews(state => state[id] ? state : { ...state, [id]: { ...DEFAULT_GRAPH_VIEW } }); }
  }, [workspace]);
  useEffect(() => {
    if (!workspace || !graphViews[workspace.id]) return;
    try { localStorage.setItem(`potbot:ui:graph-view:v1:${workspace.id}`,
      JSON.stringify(graphViews[workspace.id])); } catch { /* Storage is optional. */ }
  }, [workspace, graphViews]);
  useEffect(() => {
    if (!workspace) return;
    const id = workspace.id;
    try {
      const compact = localStorage.getItem(`potbot:ui:pref:compact:${id}`) === "1";
      setCompactPrefs(state => id in state ? state : { ...state, [id]: compact });
    } catch { setCompactPrefs(state => id in state ? state : { ...state, [id]: false }); }
  }, [workspace]);
  useEffect(() => {
    if (!workspace) return;
    document.body.classList.toggle("potbot-compact", compactPrefs[workspace.id] === true);
    return () => document.body.classList.remove("potbot-compact");
  }, [workspace, compactPrefs]);
  const setCompactPreference = useCallback((enabled: boolean) => {
    if (!workspace) return;
    setCompactPrefs(state => ({ ...state, [workspace.id]: enabled }));
    try { localStorage.setItem(`potbot:ui:pref:compact:${workspace.id}`, enabled ? "1" : "0"); } catch {}
  }, [workspace]);

  const updateGraphView = useCallback((patch: Partial<GraphView>) => {
    if (!workspace) return;
    setGraphViews(state => ({ ...state, [workspace.id]: sanitizedGraphView({
      ...(state[workspace.id] ?? DEFAULT_GRAPH_VIEW), ...patch,
    }) }));
  }, [workspace]);

  const selectWorkspace = useCallback((index: number) => {
    if (index < 0 || index >= workspaces.length) return;
    setGraphFullscreen(false);
    apiRef.current = null;
    setSelected(index);
    try { localStorage.setItem("potbot:ui:selected-workspace:v1", workspaces[index].id); } catch {}
  }, [workspaces]);

  const onXWindowHidden = useCallback((windowId: string) => {
    if (!workspace || !XWINDOW_ID.test(windowId)) return;
    (xHidden.current[workspace.id] ??= new Set()).add(windowId);
  }, [workspace]);

  const openXWindow = useCallback((windowId: string) => {
    if (!workspace || !XWINDOW_ID.test(windowId)) return;
    const item = (xWindowsByWorkspace[workspace.id] ?? []).find(w => w.id === windowId);
    const api = apiRef.current;
    if (!item || !api) return;
    (xHidden.current[workspace.id] ??= new Set()).delete(windowId);
    (xSeen.current[workspace.id] ??= new Set()).add(windowId);
    const id = windowPanelId(windowId);
    const existing = api.getPanel(id);
    if (existing) { existing.api.setActive(); return; }
    const width = Math.min(item.width + 36, 750, Math.max(280, window.innerWidth - 48));
    const height = Math.min(item.height + 80, 570, Math.max(200, window.innerHeight - 150));
    api.addPanel({ id, component: "xwindow", title: item.title, params: { windowId },
      floating: { position: { top: 105, right: 40 }, width, height },
      minimumHeight: 160, minimumWidth: 230 });
  }, [workspace, xWindowsByWorkspace]);

  const closeXWindow = useCallback((windowId: string) => {
    if (!workspace || !XWINDOW_ID.test(windowId)) return;
    onXWindowHidden(windowId);
    const api = apiRef.current;
    const pane = api?.getPanel(windowPanelId(windowId));
    if (pane) api?.removePanel(pane);
    // Closing a Dockview surface must NOT terminate the underlying X process.
  }, [workspace, onXWindowHidden]);

  // Xpra owns a single binary connection, one decoded canvas per application
  // window. No Xvfb, XTEST or HTTP screenshot polling exists in this transport.
  const sendXpra = useCallback((windowId: string, command: XpraControl) => {
    if (!XWINDOW_ID.test(windowId) || !validXpraControl(command)) return;
    xpraIframe.current?.contentWindow?.postMessage({
      type: "potbot:xpra-control", channel: xpraChannel.current, id: windowId, ...command,
    }, window.location.origin);
  }, []);

  useEffect(() => {
    if (demo || gateway !== "ready" || !workspace) return;
    const workspaceId = workspace.id;
    let alive = true;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let reconnects = 0;
    const requestTicket = async () => {
      if (!alive) return;
      try {
        const response = await fetch("/api/ui/xpra/ticket", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" }, body: "{}", cache: "no-store" });
        if (response.status === 401) { setGateway("login"); return; }
        if (!response.ok) throw new Error("Xpra relay unavailable");
        const document = await response.json() as { workspaceId?: string; wsUrl?: string;
          transport?: string; expiresInSeconds?: number };
        if (!alive || document.workspaceId !== workspaceId || document.transport !== "xpra-native-binary" ||
            document.expiresInSeconds !== 30 || typeof document.wsUrl !== "string") throw new Error("Invalid Xpra scope");
        const channel = Array.from(crypto.getRandomValues(new Uint8Array(16)))
          .map(n => n.toString(16).padStart(2, "0")).join("");
        xpraChannel.current = channel;
        setXpraIframeSrc(xpraFrameURL(document.wsUrl, channel));
      } catch {
        if (!alive) return;
        setXAvailable(false);
        if (reconnects++ < 8) retry = setTimeout(requestTicket, Math.min(15000, 1200 * 2 ** reconnects));
      }
    };
    const message = (event: MessageEvent) => {
      if (!alive || event.origin !== location.origin ||
          event.source !== xpraIframe.current?.contentWindow) return;
      const payload = event.data as Record<string, unknown> | null;
      if (!payload || payload.type !== "potbot:xpra" || payload.channel !== xpraChannel.current) return;
      if (payload.action === "ready") { setXAvailable(true);
        setXTransportByWorkspace(old => ({ ...old, [workspaceId]: "xpra-native-binary" }));
        reconnects = 0; return; }
      if (payload.action === "disconnected") {
        setXAvailable(false);
        setXpraIframeSrc(null);
        if (reconnects++ < 8) retry = setTimeout(requestTicket, Math.min(15000, 1200 * 2 ** reconnects));
        return;
      }
      if (payload.action === "frame" && typeof payload.id === "string" &&
          XWINDOW_ID.test(payload.id) && payload.bitmap instanceof ImageBitmap) {
        deliverXpraFrame(payload.id, payload.bitmap);
        return;
      }
      if (payload.action === "close" && typeof payload.id === "string" && XWINDOW_ID.test(payload.id)) {
        setXWindowsByWorkspace(state => ({ ...state, [workspaceId]:
          (state[workspaceId] || []).filter(item => item.id !== payload.id) }));
        const pane = apiRef.current?.getPanel(windowPanelId(payload.id));
        if (pane) apiRef.current?.removePanel(pane);
        xSeen.current[workspaceId]?.delete(payload.id);
        xHidden.current[workspaceId]?.delete(payload.id);
        return;
      }
      if (payload.action === "open") {
        const item = validXpraWindow(payload);
        if (!item) return;
        setXWindowsByWorkspace(state => ({ ...state, [workspaceId]: [
          ...(state[workspaceId] || []).filter(w => w.id !== item.id), item,
        ] }));
        const api = apiRef.current;
        const seen = xSeen.current[workspaceId] ??= new Set();
        const hidden = xHidden.current[workspaceId] ??= new Set();
        if (api && !seen.has(item.id) && !hidden.has(item.id)) {
          seen.add(item.id);
          if (!api.getPanel(windowPanelId(item.id))) api.addPanel({
            id: windowPanelId(item.id), component: "xwindow", title: item.title,
            params: { windowId: item.id },
            floating: { position: { top: 135, right: 30 },
              width: Math.min(item.width + 42, 700, Math.max(270, innerWidth - 48)),
              height: Math.min(item.height + 76, 540, Math.max(180, innerHeight - 160)) },
            minimumWidth: 230, minimumHeight: 160,
          });
        }
      }
    };
    window.addEventListener("message", message);
    void requestTicket();
    return () => { alive = false; if (retry) clearTimeout(retry);
      window.removeEventListener("message", message); setXpraIframeSrc(null); setXAvailable(false); };
  }, [demo, gateway, workspace?.id]);

  const openPanel = useCallback((id: PanelId) => {
    const api = apiRef.current;
    if (!api) return;
    const current = api.getPanel(id);
    if (current) current.api.setActive();
    else initialPanel(api, id);
    if (id === "graph") setGraphMinimized(false);
  }, []);
  const closePanel = useCallback((id: PanelId) => {
    if (id === "chat") return;
    const api = apiRef.current;
    const panel = api?.getPanel(id);
    if (panel) api?.removePanel(panel);
    if (id === "graph") setGraphMinimized(true);
  }, []);
  const dockPanel = useCallback((id: PanelId, edge: DockEdge) => {
    const api = apiRef.current;
    const panel = api?.getPanel(id), chat = api?.getPanel("chat");
    if (!panel || !chat || id === "chat") return;
    panel.api.moveTo({ group: chat.group, position: edge === "above" ? "top" : edge === "below" ? "bottom" : edge });
  }, []);
  const floatPanel = useCallback((id: PanelId) => {
    const api = apiRef.current;
    const panel = api?.getPanel(id);
    if (!panel || id === "chat") return;
    api?.addFloatingGroup(panel, floatingDefaults(id));
  }, []);

  const sendMessage = useCallback((text: string) => {
    if (!workspace) return;
    const workspaceId = workspace.id;
    if (requests.current.has(workspaceId)) return; // One live stream per workspace.
    const userId = crypto.randomUUID(), assistantId = crypto.randomUUID();
    const previous = (messages[workspaceId] ?? []).filter(item => item.role !== "status" && item.text.trim());
    const recent = [...previous, { id: userId, role: "user" as const, text }].slice(-24);
    setMessages(state => ({ ...state, [workspaceId]: [
      ...(state[workspaceId] ?? []), { id: userId, role: "user", text },
      ...(demo ? [{ id: assistantId, role: "status" as const,
        text: "UI preview only — this message was not sent to Potbot." }] :
        [{ id: assistantId, role: "assistant" as const, text: "" }]),
    ] }));
    if (demo) return;
    const controller = new AbortController();
    requests.current.set(workspaceId, controller);
    setBusy(state => ({ ...state, [workspaceId]: true }));
    let pending = "", frame = 0, finished = false;
    const flush = () => {
      if (frame) { cancelAnimationFrame(frame); frame = 0; }
      const delta = pending;
      pending = "";
      if (delta) setMessages(state => ({ ...state, [workspaceId]: (state[workspaceId] ?? []).map(item =>
        item.id === assistantId ? { ...item, text: item.text + delta } : item), }));
    };
    const status = (message: string) => setMessages(state => ({ ...state, [workspaceId]: [
      ...(state[workspaceId] ?? []).filter(item => item.id !== assistantId || item.text),
      { id: crypto.randomUUID(), role: "status", text: message },
    ] }));
    void (async () => {
      try {
        const response = await fetch("/api/ui/chat", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" }, signal: controller.signal,
          body: JSON.stringify({ workspaceId, messages: recent.map(item => ({ role: item.role, content: item.text })) }),
        });
        if (response.status === 401) {
          setGateway("login"); setWorkspaces([]);
          throw new Error("Operator session expired");
        }
        if (!response.ok || !response.body) throw new Error("Potbot did not accept this request.");
        const runId = response.headers.get("X-Potbot-Run-Id");
        if (runId && RUN_ID.test(runId)) {
          cursors.current[runId] = 0;
          setRunIds(state => ({ ...state, [workspaceId]: runId }));
          setGraphHealth(state => ({ ...state, [workspaceId]: "loading" }));
          setGraphTimelines(state => { const copy = { ...state }; delete copy[workspaceId]; return copy; });
        }
        await readChatStream(response.body, event => {
          if (event.type === "delta") {
            pending += event.text;
            if (!frame) frame = requestAnimationFrame(flush);
          } else if (event.type === "complete") { flush(); finished = true; }
          else { flush(); status(event.message); }
        });
        if (!finished) return; // The parser already recorded uncertainty.
      } catch { flush(); status("Potbot connection failed; the inference outcome may be uncertain."); }
      finally {
        flush(); requests.current.delete(workspaceId);
        setBusy(state => ({ ...state, [workspaceId]: false }));
      }
    })();
  }, [workspace, messages, demo]);

  const runId = workspace ? runIds[workspace.id] : null;
  useEffect(() => {
    if (demo || !workspace || !runId || gateway !== "ready") return;
    const workspaceId = workspace.id;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const poll = async () => {
      if (!alive || document.hidden) { timer = setTimeout(poll, 4000); return; }
      try {
        const after = cursors.current[runId] ?? 0;
        const response = await fetch(`/api/ui/reasoning/${runId}?after=${after}`, {
          signal: controller.signal, cache: "no-store",
        });
        if (response.status === 401) {
          setGateway("login"); setWorkspaces([]); return;
        }
        if (!response.ok) throw new Error("Reasoning projection unavailable");
        const document = await response.json() as {
          graph?: GraphProjection; cursor?: number; gap?: boolean; more?: boolean; status?: string;
          events?: GraphEvent[];
        };
        if (!alive || !document.graph || !Number.isSafeInteger(document.cursor)) throw new Error("Incomplete graph event page");
        // Snapshot is already refreshed. A retention gap must not cause an
        // infinite after=0 replay loop; keep the last available cursor.
        cursors.current[runId] = document.cursor!;
        const events = document.events;
        if (Array.isArray(events)) setGraphTimelines(state => {
          const merged = mergeGraphTimeline(state[workspaceId] ?? null, runId, events, document.gap);
          return merged ? { ...state, [workspaceId]: merged } : state;
        });
        setGraphs(prev => {
          const existing = prev[workspaceId];
          // Avoid reconstructing a Three.js renderer on every no-op poll.
          if (existing && JSON.stringify(existing) === JSON.stringify(document.graph)) return prev;
          return { ...prev, [workspaceId]: document.graph! };
        });
        setGraphHealth(prev => ({ ...prev, [workspaceId]: document.gap ? "source event history incomplete" : document.status || "ready" }));
        timer = setTimeout(poll, document.gap || document.more ? 300 :
          graphMinimized || ["completed", "failed", "indeterminate", "cancelled"].includes(document.status || "") ? 15000 : 3000);
      } catch {
        if (!alive) return;
        setGraphHealth(prev => ({ ...prev, [workspaceId]: "unavailable — retrying" }));
        timer = setTimeout(poll, 5000);
      }
    };
    void poll();
    return () => { alive = false; controller.abort(); clearTimeout(timer); };
  }, [demo, gateway, runId, workspace, graphMinimized]);

  const logout = useCallback(() => {
    for (const controller of requests.current.values()) controller.abort();
    requests.current.clear();
    void fetch("/api/ui/auth/logout", { method: "POST", credentials: "same-origin" }).then(response => {
      if (!response.ok) throw new Error("Logout not acknowledged");
      setGateway("login"); setWorkspaces([]); setMessages({}); setRunIds({}); setGraphs({});
      setGraphHealth({}); setGraphTimelines({}); setGraphViews({}); setCompactPrefs({});
      setXAvailable(false); setXWindowsByWorkspace({}); setXTransportByWorkspace({});
      xSeen.current = {}; xHidden.current = {};
      setPassword(""); setSelected(0); apiRef.current = null;
    }).catch(() => setNotice("Logout failed; your existing operator session may still be active."));
  }, []);

  const session = useMemo(() => workspace ? ({ workspace, demo, isLive: !demo,
    entries: messages[workspace.id] ?? [], chatBusy: !!busy[workspace.id],
    graph: demo ? null : graphs[workspace.id] ?? null,
    graphState: demo ? "synthetic preview" : graphHealth[workspace.id] || "No active reasoning run",
    graphTimeline: demo ? null : graphTimelines[workspace.id] ?? null,
    graphView: graphViews[workspace.id] ?? { ...DEFAULT_GRAPH_VIEW },
    compact: compactPrefs[workspace.id] === true,
    setCompactPreference, updateGraphView,
    runId: runIds[workspace.id] ?? null,
    xAvailable, xTransport: xTransportByWorkspace[workspace.id] ?? null,
    xWindows: xWindowsByWorkspace[workspace.id] ?? [],
    sendXpra,
    openXWindow, closeXWindow,
    logout, sendMessage, openPanel, closePanel, dockPanel,
    floatPanel, graphFullscreen, setGraphFullscreen }) : null,
    [workspace, demo, messages, busy, graphs, graphHealth, graphTimelines, graphViews,
      compactPrefs, setCompactPreference, updateGraphView, runIds, logout, sendMessage, openPanel, closePanel,
      dockPanel, floatPanel, graphFullscreen, xAvailable, xTransportByWorkspace,
      xWindowsByWorkspace, sendXpra,
      openXWindow, closeXWindow]);

  if (!workspace || !session) return <main className="setup-screen">
    <span className="setup-icon">✳</span><h1>Potbot Orb Workspace</h1>
    {demo ? <p>No demonstration workspaces are available.</p> : gateway === "checking" ?
      <p>Checking the operator session and authorized workspace…</p> : gateway === "login" ?
      <form className="operator-login" onSubmit={async event => {
        event.preventDefault(); setNotice("");
        try {
          const response = await fetch("/api/ui/auth/login", { method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) });
          if (!response.ok) throw new Error("Invalid credentials or rate limit");
          setPassword(""); setGateway("checking"); await loadLive();
        } catch { setNotice("Unable to log in. Check your operator credentials or try again later."); }
      }}><label htmlFor="operator-password">Operator password</label>
        <input id="operator-password" type="password" required value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" />
        <button type="submit">Unlock workspace</button>
      </form> : <><p>{notice || "Potbot workspace connection has not been established."}</p>
        <button className="demo-button" onClick={() => { setGateway("checking"); void loadLive(); }}>Retry connection</button></>}
    {gateway === "login" && notice && <p role="alert">{notice}</p>}
    <span className="status-chip">{demo ? "DEMO MODE" : gateway === "login" ? "OPERATOR LOGIN" : gateway.toUpperCase()}</span>
    {process.env.NODE_ENV !== "production" && <a className="demo-button" href="/?demo=1">Open interactive demo →</a>}
    <small>Live mode never substitutes synthetic workspaces when the backend is unavailable.</small>
  </main>;

  return <WorkspaceProvider value={session}>
    <LayoutGroup id={`potbot-${workspace.id}`}>
    <main className="potbot-scene">
      <WorkspaceOrbit workspaces={workspaces} selected={selected} onSelect={selectWorkspace} />
      <button className="top-menu-toggle" onClick={() => openPanel("menu")}
        aria-label="Open control menu" title="Open menu">☰ <span>Menu</span></button>
      <div className="utility-rail" aria-label="Utility panes">
        {graphMinimized && <motion.button layoutId="potbot-graph-window" className="utility-orb" onClick={() => openPanel("graph")}
          aria-label="Restore reasoning graph" title="Restore thought graph">◇</motion.button>}
        {!demo && (xWindowsByWorkspace[workspace.id] ?? []).slice(0, 5).map(win =>
          <button key={win.id} className="utility-small" onClick={() => openXWindow(win.id)}
            title={`Open Xpra application: ${win.title}`} aria-label={`Open Xpra window ${win.title}`}>▣</button>)}
        <button onClick={() => openPanel("preferences")} className="utility-small" aria-label="Preferences" title="Preferences">⚙</button>
      </div>
      <div className="scene-presence"><span className="presence-light" />{demo ? "INTERACTIVE PREVIEW" : "SCOPED LIVE CONNECTION"}</div>
      {!demo && xpraIframeSrc && <iframe ref={xpraIframe} src={xpraIframeSrc}
        title="Workspace Xpra protocol and decoding engine" className="xpra-protocol-frame"
        aria-hidden="true" tabIndex={-1} sandbox="allow-scripts allow-same-origin" />}
      <DockApiContext.Provider value={apiRef}>
        <div className="dock-root" key={workspace.id}>
          <DockedScene workspace={workspace} onGraphMinimized={setGraphMinimized}
            onXWindowHidden={onXWindowHidden} />
        </div>
      </DockApiContext.Provider>
      <AnimatePresence>
        {graphFullscreen && <motion.div className="fullscreen-layer" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <FullscreenGraph />
        </motion.div>}
      </AnimatePresence>
    </main>
    </LayoutGroup>
  </WorkspaceProvider>;
}
