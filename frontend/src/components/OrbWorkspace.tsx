"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { DockviewApi, DockviewReadyEvent } from "dockview-react";
import { DockviewReact } from "dockview-react";
import { AnimatePresence, LayoutGroup, MotionConfig, motion } from "motion/react";
import { bootWorkspaces } from "@/lib/boot";
import { readChatStream } from "@/lib/chat-stream";
import { conversationEntries, projectSession, projectSessionList } from "@/lib/chat-sessions";
import type { ChatSessionSummary } from "@/lib/chat-sessions";
import { projectAppInventory, validAppKind, appSessionId } from "@/lib/xpra-apps";
import type { AppInventory } from "@/lib/xpra-apps";
import { DEFAULT_GRAPH_VIEW, mergeGraphTimeline, sanitizedGraphView } from "@/lib/graph-workbench";
import type { GraphView, Timeline, GraphEvent } from "@/lib/graph-workbench";
import { RUN_ID } from "@/lib/live-schema";
import { validXpraWindow, xpraFrameURL, validXpraControl, windowPanelId, XWINDOW_ID } from "@/lib/xpra-contract";
import { deliverXpraFrame } from "@/lib/xpra-frames";
import type { ActivityPage, ModelListing } from "@/lib/activity";
import { projectPreferences, projectTransfers } from "@/lib/ui-resources";
import type { UiPreferences, UiSettings, TransferProgress, ArtifactManifest } from "@/lib/ui-resources";
import type { XWindowInfo, XWindowTransport, XpraControl } from "@/lib/xpra-contract";
import { decodeSavedLayout, layoutStorageKey, PANEL_TITLES, shouldMinimizePanel } from "@/lib/layout";
import type { ChatEntry, DockEdge, PanelId, Workspace, GraphProjection } from "@/lib/model";
import { WorkspaceOrbit } from "./WorkspaceOrbit";
import { WorkspaceProvider } from "./WorkspaceContext";
import { XpraWindowPanel } from "./XpraWindowPanel";
import { XpraApplicationsPanel } from "./XpraApplicationsPanel";
import { ActivityPanel } from "./ActivityPanel";
import { BrowserPanel, ChatPanel, DownloadsPanel, FullscreenGraph, GraphPanel, InstallationsPanel, MenuPanel, PreferencesPanel, TerminalPanel } from "./Panels";

const COMPONENTS = {
  chat: ChatPanel, graph: GraphPanel, menu: MenuPanel, terminal: TerminalPanel,
  browser: BrowserPanel, preferences: PreferencesPanel, downloads: DownloadsPanel,
  installations: InstallationsPanel, applications: XpraApplicationsPanel, activity: ActivityPanel, xwindow: XpraWindowPanel,
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
  const [chatSessions, setChatSessions] = useState<Record<string, ChatSessionSummary[]>>({});
  const [activeChatIds, setActiveChatIds] = useState<Record<string, string>>({});
  const [chatHistoryStatus, setChatHistoryStatus] = useState<Record<string, string>>({});
  const chatLoadGenerations = useRef<Record<string, number>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [runIds, setRunIds] = useState<Record<string, string>>({});
  const [graphs, setGraphs] = useState<Record<string, GraphProjection>>({});
  const [activityFeeds, setActivityFeeds] = useState<Record<string, ActivityPage>>({});
  const [activityStatuses, setActivityStatuses] = useState<Record<string, string>>({});
  const [modelLists, setModelLists] = useState<Record<string, ModelListing[]>>({});
  const [modelStatuses, setModelStatuses] = useState<Record<string, string>>({});
  const activityCursors = useRef<Record<string, number>>({});
  const activityInFlight = useRef(new Set<string>());
  const modelInFlight = useRef(new Set<string>());
  const [remotePrefs, setRemotePrefs] = useState<Record<string, UiPreferences>>({});
  const [preferenceStatuses, setPreferenceStatuses] = useState<Record<string, string>>({});
  const [transferFeeds, setTransferFeeds] = useState<Record<string, TransferProgress[]>>({});
  const [transferStatuses, setTransferStatuses] = useState<Record<string, string>>({});
  const preferenceInFlight = useRef(new Set<string>());
  const transferInFlight = useRef(new Set<string>());
  const sessionGeneration = useRef(0);
  const [appInventories, setAppInventories] = useState<Record<string, AppInventory>>({});
  const [appStatuses, setAppStatuses] = useState<Record<string, string>>({});
  const appsInFlight = useRef(new Set<string>());
  const [xWindowsByWorkspace, setXWindowsByWorkspace] = useState<Record<string, XWindowInfo[]>>({});
  const [xErrorsByWorkspace, setXErrorsByWorkspace] = useState<Record<string, Record<string, string>>>({});
  const [xMinimizedByWorkspace, setXMinimizedByWorkspace] = useState<Record<string, string[]>>({});
  const [xReconnectByWorkspace, setXReconnectByWorkspace] = useState<Record<string, number>>({});
  const [xpraRetryNonce, setXpraRetryNonce] = useState(0);
  const [xAvailable, setXAvailable] = useState(false);
  const [xpraIframeSrc, setXpraIframeSrc] = useState<string | null>(null);
  const xpraIframe = useRef<HTMLIFrameElement | null>(null);
  const xpraChannel = useRef("");
  const [xTransportByWorkspace, setXTransportByWorkspace] = useState<Record<string, XWindowTransport | null>>({});
  const xSeen = useRef<Record<string, Set<string>>>({});
  const xHidden = useRef<Record<string, Set<string>>>({});
  const xGeneration = useRef<Record<string, Map<string, number>>>({});
  const xLastFullRequest = useRef<Record<string, Map<string, number>>>({});
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
  const scopedHeaders = (id: string) => ({ "X-Potbot-Workspace": id });

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

  useEffect(() => {
    if (!workspace) return;
    const theme = remotePrefs[workspace.id]?.settings.theme ?? "system";
    const reduced = remotePrefs[workspace.id]?.settings.reducedMotion === true;
    document.body.classList.toggle("potbot-theme-light", theme === "light");
    document.body.classList.toggle("potbot-reduced-motion", reduced);
    return () => { document.body.classList.remove("potbot-theme-light", "potbot-reduced-motion"); };
  }, [workspace?.id, remotePrefs]);

  useEffect(() => {
    if (!workspace || !xAvailable || demo) return;
    const fps = remotePrefs[workspace.id]?.settings.xpraMaxFps ?? 30;
    xpraIframe.current?.contentWindow?.postMessage({
      type: "potbot:xpra-control", channel: xpraChannel.current,
      action: "set-fps", fps,
    }, location.origin);
  }, [workspace?.id, remotePrefs, xAvailable, demo]);

  const selectWorkspace = useCallback((index: number) => {
    if (index < 0 || index >= workspaces.length) return;
    setGraphFullscreen(false);
    apiRef.current = null;
    setSelected(index);
    try { localStorage.setItem("potbot:ui:selected-workspace:v1", workspaces[index].id); } catch {}
  }, [workspaces]);

  const onXWindowHidden = useCallback((windowId: string) => {
    if (!workspace || !XWINDOW_ID.test(windowId)) return;
    const id = workspace.id;
    // Xpra window-destroy removes the panel too; it must NOT re-add a stale
    // minimized entry when Dockview emits its asynchronous removal callback.
    if (!xGeneration.current[id]?.has(windowId)) return;
    (xHidden.current[id] ??= new Set()).add(windowId);
    setXMinimizedByWorkspace(state => state[id]?.includes(windowId) ? state :
      { ...state, [id]: [...(state[id] ?? []), windowId] });
  }, [workspace]);

  const openXWindow = useCallback((windowId: string) => {
    if (!workspace || !XWINDOW_ID.test(windowId)) return;
    const item = (xWindowsByWorkspace[workspace.id] ?? []).find(w => w.id === windowId);
    const api = apiRef.current;
    if (!item || !api) return;
    (xHidden.current[workspace.id] ??= new Set()).delete(windowId);
    setXMinimizedByWorkspace(state => ({ ...state,
      [workspace.id]: (state[workspace.id] ?? []).filter(id => id !== windowId) }));
    (xSeen.current[workspace.id] ??= new Set()).add(windowId);
    const id = windowPanelId(windowId);
    const focusSurface = () => requestAnimationFrame(() => {
      // Window IDs are restricted to lowercase hex by XWINDOW_ID, so this
      // attribute selector is safe and cannot target another workspace's pane.
      document.querySelector<HTMLCanvasElement>(
        `.xwindow-panel[data-xpra-window-id="${windowId}"] .xpra-app-canvas`
      )?.focus();
    });
    const existing = api.getPanel(id);
    if (existing) { existing.api.setActive(); focusSurface(); return; }
    const width = Math.min(item.width + 36, 750, Math.max(280, window.innerWidth - 48));
    const height = Math.min(item.height + 80, 570, Math.max(200, window.innerHeight - 150));
    api.addPanel({ id, component: "xwindow", title: item.title, params: { windowId },
      floating: { position: { top: 105, right: 40 }, width, height },
      minimumHeight: 160, minimumWidth: 230 });
    focusSurface();
  }, [workspace, xWindowsByWorkspace]);

  const closeXWindow = useCallback((windowId: string) => {
    if (!workspace || !XWINDOW_ID.test(windowId)) return;
    onXWindowHidden(windowId);
    const api = apiRef.current;
    const pane = api?.getPanel(windowPanelId(windowId));
    if (pane) api?.removePanel(pane);
    // Closing a Dockview surface must NOT terminate the underlying X process.
  }, [workspace, onXWindowHidden]);

  const retryXpra = useCallback(() => {
    if (demo || gateway !== "ready") return;
    setXAvailable(false);
    setXpraIframeSrc(null);
    setXpraRetryNonce(value => value + 1);
  }, [demo, gateway]);

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
    setXReconnectByWorkspace(state => ({ ...state, [workspaceId]: 0 }));
    const requestTicket = async () => {
      if (!alive) return;
      try {
        const response = await fetch("/api/ui/xpra/ticket", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...scopedHeaders(workspaceId) }, body: "{}", cache: "no-store" });
        if (response.status === 401) { setGateway("login"); return; }
        if (response.status === 404) { setXAvailable(false); return; } // No Xpra relay belongs to this workspace.
        if (!response.ok) throw new Error("Xpra relay unavailable");
        const document = await response.json() as { workspaceId?: string; wsUrl?: string;
          transport?: string; expiresInSeconds?: number };
        if (!alive || document.workspaceId !== workspaceId || document.transport !== "xpra-native-binary" ||
            document.expiresInSeconds !== 30 || typeof document.wsUrl !== "string") throw new Error("Invalid Xpra scope");
        const channel = Array.from(crypto.getRandomValues(new Uint8Array(16)))
          .map(n => n.toString(16).padStart(2, "0")).join("");
        xpraChannel.current = channel;
        // A new Xpra connection may reuse numeric X window IDs; old frames
        // must never paint into the replacement application's surface.
        xGeneration.current[workspaceId] = new Map();
        xLastFullRequest.current[workspaceId] = new Map();
        setXpraIframeSrc(xpraFrameURL(document.wsUrl, channel));
      } catch {
        if (!alive) return;
        setXAvailable(false);
        if (reconnects++ < 8) {
          setXReconnectByWorkspace(state => ({ ...state, [workspaceId]: reconnects }));
          retry = setTimeout(requestTicket, Math.min(15000, 1200 * 2 ** reconnects));
        }
      }
    };
    const message = (event: MessageEvent) => {
      if (!alive || event.origin !== location.origin ||
          event.source !== xpraIframe.current?.contentWindow) return;
      const payload = event.data as Record<string, unknown> | null;
      if (!payload || payload.type !== "potbot:xpra" || payload.channel !== xpraChannel.current) return;
      if (payload.action === "ready") { setXAvailable(true);
        setXTransportByWorkspace(old => ({ ...old, [workspaceId]: "xpra-native-binary" }));
        reconnects = 0;
        setXReconnectByWorkspace(state => ({ ...state, [workspaceId]: 0 }));
        return; }
      if (payload.action === "disconnected") {
        setXAvailable(false);
        setXpraIframeSrc(null);
        if (reconnects++ < 8) {
          setXReconnectByWorkspace(state => ({ ...state, [workspaceId]: reconnects }));
          retry = setTimeout(requestTicket, Math.min(15000, 1200 * 2 ** reconnects));
        }
        return;
      }
      if (payload.action === "error" && typeof payload.id === "string" &&
          XWINDOW_ID.test(payload.id) && payload.reason === "window-bitmap-unavailable" &&
          xGeneration.current[workspaceId]?.get(payload.id) === payload.generation) {
        const id = payload.id;
        setXErrorsByWorkspace(state => ({ ...state, [workspaceId]: {
          ...state[workspaceId], [id]: "Xpra decoder cannot capture this window. Retry the full frame or check the backend decoder.",
        } }));
        return;
      }
      if (payload.action === "frame" && typeof payload.id === "string" &&
          XWINDOW_ID.test(payload.id) && typeof ImageBitmap !== "undefined" &&
          payload.bitmap instanceof ImageBitmap) {
        const expected = xGeneration.current[workspaceId]?.get(payload.id);
        if (payload.generation !== expected) { payload.bitmap.close(); return; }
        const id = payload.id;
        const disposition = deliverXpraFrame(id, payload.bitmap, payload);
        if (disposition === "applied") setXErrorsByWorkspace(state => {
          if (!state[workspaceId]?.[id]) return state;
          const next = { ...state[workspaceId] };
          delete next[id];
          return { ...state, [workspaceId]: next };
        });
        if (disposition === "resync") {
          const last = xLastFullRequest.current[workspaceId] ??= new Map();
          const now = performance.now();
          if (now - (last.get(id) ?? -1000) > 500) {
            last.set(id, now);
            sendXpra(id, { action: "frame" });
          }
        }
        return;
      }
      if (payload.action === "close" && typeof payload.id === "string" && XWINDOW_ID.test(payload.id)) {
        if (xGeneration.current[workspaceId]?.get(payload.id) !== payload.generation) return;
        const id = payload.id;
        xGeneration.current[workspaceId]?.delete(id);
        xLastFullRequest.current[workspaceId]?.delete(id);
        setXErrorsByWorkspace(state => {
          if (!state[workspaceId]?.[id]) return state;
          const next = { ...state[workspaceId] };
          delete next[id];
          return { ...state, [workspaceId]: next };
        });
        setXWindowsByWorkspace(state => ({ ...state, [workspaceId]:
          (state[workspaceId] || []).filter(item => item.id !== id) }));
        const pane = apiRef.current?.getPanel(windowPanelId(id));
        if (pane) apiRef.current?.removePanel(pane);
        xSeen.current[workspaceId]?.delete(id);
        xHidden.current[workspaceId]?.delete(id);
        setXMinimizedByWorkspace(state => ({ ...state,
          [workspaceId]: (state[workspaceId] ?? []).filter(name => name !== id) }));
        return;
      }
      if (payload.action === "open") {
        const item = validXpraWindow(payload);
        if (!item) return;
        const previous = xGeneration.current[workspaceId] ??= new Map();
        if ((previous.get(item.id) ?? 0) > item.generation) return;
        previous.set(item.id, item.generation);
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
  }, [demo, gateway, workspace?.id, xpraRetryNonce, sendXpra]);

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

  const selectChatSession = useCallback((sessionId: string) => {
    if (demo || gateway !== "ready" || !workspace || busy[workspace.id]) return;
    const workspaceId = workspace.id;
    if (!/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(sessionId)) return;
    const generation = (chatLoadGenerations.current[workspaceId] ?? 0) + 1;
    chatLoadGenerations.current[workspaceId] = generation;
    setActiveChatIds(prev => ({ ...prev, [workspaceId]: sessionId }));
    setMessages(prev => ({ ...prev, [workspaceId]: [] }));
    setRunIds(prev => ({ ...prev, [workspaceId]: "" }));
    setChatHistoryStatus(prev => ({ ...prev, [workspaceId]: "Loading saved conversation…" }));
    try { localStorage.setItem(`potbot:ui:active-chat:v1:${workspaceId}`, sessionId); } catch {}
    void (async () => {
      try {
        const response = await fetch(`/api/ui/sessions/${sessionId}`, {
          credentials: "same-origin", cache: "no-store", headers: scopedHeaders(workspaceId),
        });
        if (!response.ok) throw new Error("Session unavailable");
        const raw = await response.json() as { workspaceId?: string };
        const details = raw.workspaceId === workspaceId ? projectSession(raw) : null;
        if (!details || chatLoadGenerations.current[workspaceId] !== generation) return;
        setMessages(prev => ({ ...prev, [workspaceId]: conversationEntries(details) }));
        const last = [...details.turns].reverse().find(turn => turn.runId);
        setRunIds(prev => ({ ...prev, [workspaceId]: last?.runId ?? "" }));
        setChatHistoryStatus(prev => ({ ...prev, [workspaceId]: "Saved conversation loaded" }));
      } catch { if (chatLoadGenerations.current[workspaceId] === generation)
        setChatHistoryStatus(prev => ({ ...prev, [workspaceId]: "Unable to load saved conversation" })); }
    })();
  }, [demo, gateway, workspace?.id, busy]);

  const newChatSession = useCallback(() => {
    if (demo || gateway !== "ready" || !workspace || busy[workspace.id]) return;
    const workspaceId = workspace.id, id = crypto.randomUUID();
    void (async () => {
      try {
        const response = await fetch("/api/ui/sessions", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...scopedHeaders(workspaceId) },
          body: JSON.stringify({ workspaceId, id, title: "New conversation" }),
        });
        if (!response.ok) throw new Error("New chat unavailable");
        setChatSessions(prev => ({ ...prev, [workspaceId]: [{ id, title: "New conversation", revision: 0,
          updatedAt: new Date().toISOString() }, ...(prev[workspaceId] ?? [])].slice(0, 50) }));
        selectChatSession(id);
      } catch { setChatHistoryStatus(prev => ({ ...prev, [workspaceId]: "Unable to create a chat session" })); }
    })();
  }, [demo, gateway, workspace?.id, busy, selectChatSession]);

  useEffect(() => {
    if (demo || gateway !== "ready" || !workspace) return;
    const workspaceId = workspace.id;
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch("/api/ui/sessions", {
          credentials: "same-origin", cache: "no-store", headers: scopedHeaders(workspaceId),
        });
        if (!response.ok) throw new Error("Chat list unavailable");
        const raw = await response.json() as { workspaceId?: string };
        const sessions = raw.workspaceId === workspaceId ? projectSessionList(raw) : null;
        if (!sessions || cancelled) throw new Error("Invalid chat list");
        setChatSessions(prev => ({ ...prev, [workspaceId]: sessions }));
        let selected = "";
        try { selected = localStorage.getItem(`potbot:ui:active-chat:v1:${workspaceId}`) || ""; } catch {}
        const chosen = sessions.some(s => s.id === selected) ? selected : sessions[0]?.id;
        if (chosen) selectChatSession(chosen);
        else {
          setMessages(prev => ({ ...prev, [workspaceId]: [] }));
          setChatHistoryStatus(prev => ({ ...prev, [workspaceId]: "No previous conversations" }));
        }
      } catch { if (!cancelled)
        setChatHistoryStatus(prev => ({ ...prev, [workspaceId]: "Chat history unavailable" })); }
    })();
    return () => { cancelled = true; };
  }, [demo, gateway, workspace?.id]);

  const reconcileChatSession = useCallback(() => {
    if (demo || gateway !== "ready" || !workspace || busy[workspace.id]) return;
    const workspaceId = workspace.id, chatId = activeChatIds[workspaceId];
    if (!chatId || !window.confirm("Mark this abandoned request indeterminate? It will not be retried.")) return;
    const generation = (chatLoadGenerations.current[workspaceId] ?? 0);
    void (async () => {
      try {
        const result = await fetch(`/api/ui/sessions/${chatId}`, {
          credentials: "same-origin", cache: "no-store", headers: scopedHeaders(workspaceId),
        });
        const raw = result.ok ? await result.json() as {workspaceId?: string} : null;
        const details = raw?.workspaceId === workspaceId ? projectSession(raw) : null;
        const pending = details?.turns.at(-1);
        if (!pending || pending.state !== "pending") throw new Error("No pending turn");
        const response = await fetch(`/api/ui/sessions/${chatId}/reconcile`, {
          method: "POST", credentials: "same-origin", headers: {
            "Content-Type": "application/json", ...scopedHeaders(workspaceId),
          }, body: JSON.stringify({ workspaceId, turnId: pending.id, confirm: true }),
        });
        if (!response.ok) throw new Error("Turn is still active or unavailable");
        if (chatLoadGenerations.current[workspaceId] === generation) selectChatSession(chatId);
      } catch { setChatHistoryStatus(prev => ({ ...prev, [workspaceId]:
        "Cannot reconcile this turn yet; it may still be running or needs operator investigation." })); }
    })();
  }, [demo, gateway, workspace?.id, busy, activeChatIds, selectChatSession]);

  const sendMessage = useCallback((text: string) => {
    if (!workspace) return;
    const workspaceId = workspace.id;
    if (requests.current.has(workspaceId)) return; // One live stream per workspace.
    // Invalidate a delayed history load before optimistic user messages appear.
    chatLoadGenerations.current[workspaceId] = (chatLoadGenerations.current[workspaceId] ?? 0) + 1;
    const userId = crypto.randomUUID(), assistantId = crypto.randomUUID();
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
        let sessionId = activeChatIds[workspaceId];
        if (!sessionId) {
          sessionId = crypto.randomUUID();
          const title = text.slice(0, 100);
          const created = await fetch("/api/ui/sessions", { method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json", ...scopedHeaders(workspaceId) }, signal: controller.signal,
            body: JSON.stringify({ workspaceId, id: sessionId, title }),
          });
          if (!created.ok) throw new Error("Chat session creation failed");
          setActiveChatIds(old => ({ ...old, [workspaceId]: sessionId! }));
          try { localStorage.setItem(`potbot:ui:active-chat:v1:${workspaceId}`, sessionId); } catch {}
          setChatSessions(old => ({ ...old, [workspaceId]: [{ id: sessionId!, title, revision: 0,
            updatedAt: new Date().toISOString() }, ...(old[workspaceId] ?? [])] }));
        }
        const response = await fetch(`/api/ui/sessions/${sessionId}/send`, { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...scopedHeaders(workspaceId) }, signal: controller.signal,
          body: JSON.stringify({ workspaceId, requestId: userId, text }),
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
        // [DONE] arrives only after the BFF has persisted the assistant response.
        const saved = await fetch(`/api/ui/sessions/${sessionId}`, {
          credentials: "same-origin", cache: "no-store", headers: scopedHeaders(workspaceId),
          signal: controller.signal,
        });
        if (saved.ok) {
          const raw = await saved.json() as { workspaceId?: string };
          const history = raw.workspaceId === workspaceId ? projectSession(raw) : null;
          if (history) setMessages(old => ({ ...old, [workspaceId]: conversationEntries(history) }));
        }
      } catch { flush(); status("Potbot connection failed; the inference outcome may be uncertain."); }
      finally {
        flush(); requests.current.delete(workspaceId);
        setBusy(state => ({ ...state, [workspaceId]: false }));
      }
    })();
  }, [workspace, activeChatIds, demo]);

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
          signal: controller.signal, cache: "no-store", headers: scopedHeaders(workspaceId),
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

  // Read-only, workspace-scoped activity feed. No raw job inputs or outcomes.
  const refreshActivity = useCallback(() => {
    if (demo || gateway !== "ready" || !workspace) return;
    const id = workspace.id;
    if (activityInFlight.current.has(id)) return;
    activityInFlight.current.add(id);
    const generation = sessionGeneration.current;
    const after = activityCursors.current[id] ?? 0;
    void (async () => {
      try {
        const response = await fetch(`/api/ui/activity?after=${after}`, {
          credentials: "same-origin", cache: "no-store", headers: scopedHeaders(id),
        });
        if (response.status === 401) { if (sessionGeneration.current === generation) setGateway("login"); return; }
        if (!response.ok) throw new Error("Activity backend unavailable");
        const data = await response.json() as ActivityPage & { workspaceId?: string };
        if (data.workspaceId !== id || !Array.isArray(data.jobs) || !Array.isArray(data.events) ||
            !Number.isSafeInteger(data.cursor) || data.cursor < after ||
            data.transferTelemetry !== false || data.installTelemetry !== false) throw new Error("Malformed activity response");
        if (sessionGeneration.current !== generation) return;
        activityCursors.current[id] = data.cursor;
        setActivityFeeds(previous => {
          const old = previous[id];
          const events = [...(old?.events ?? []), ...data.events].filter((entry, index, all) =>
            all.findIndex(other => other.sequence === entry.sequence) === index).slice(-100);
          return { ...previous, [id]: { ...data, events, gap: data.gap || (old?.gap ?? false) } };
        });
        setActivityStatuses(old => ({ ...old, [id]: data.gap ? "Event history incomplete; current jobs verified." : "Live job feed connected" }));
      } catch {
        if (sessionGeneration.current === generation)
          setActivityStatuses(old => ({ ...old, [id]: "Activity temporarily unavailable — retrying" }));
      } finally { activityInFlight.current.delete(id); }
    })();
  }, [demo, gateway, workspace?.id]);

  const refreshModels = useCallback(() => {
    if (demo || gateway !== "ready" || !workspace) return;
    const id = workspace.id;
    if (modelInFlight.current.has(id)) return;
    modelInFlight.current.add(id);
    const generation = sessionGeneration.current;
    void (async () => {
      try {
        const response = await fetch("/api/ui/models", { credentials: "same-origin", cache: "no-store", headers: scopedHeaders(id) });
        if (response.status === 401) { if (sessionGeneration.current === generation) setGateway("login"); return; }
        if (!response.ok) throw new Error("Model catalogue unavailable");
        const data = await response.json() as { workspaceId?: string; models?: ModelListing[]; advisory?: boolean; installTelemetry?: boolean };
        if (data.workspaceId !== id || !Array.isArray(data.models) || data.advisory !== true ||
            data.installTelemetry !== false) throw new Error("Model catalogue scope mismatch");
        if (sessionGeneration.current !== generation) return;
        setModelLists(old => ({ ...old, [id]: data.models ?? [] }));
        setModelStatuses(old => ({ ...old, [id]: "Live advisory catalogue · installation state not reported" }));
      } catch {
        if (sessionGeneration.current === generation)
          setModelStatuses(old => ({ ...old, [id]: "Catalogue temporarily unavailable — retry manually" }));
      } finally { modelInFlight.current.delete(id); }
    })();
  }, [demo, gateway, workspace?.id]);

  const refreshPreferences = useCallback(() => {
    if (demo || gateway !== "ready" || !workspace) return;
    const id = workspace.id;
    if (preferenceInFlight.current.has(id)) return;
    preferenceInFlight.current.add(id);
    const generation = sessionGeneration.current;
    void (async () => {
      try {
        const response = await fetch("/api/ui/preferences", { cache: "no-store", credentials: "same-origin", headers: scopedHeaders(id) });
        if (response.status === 401) { if (sessionGeneration.current === generation) setGateway("login"); return; }
        if (!response.ok) throw new Error("Workspace preferences endpoint unavailable");
        const data = projectPreferences(await response.json(), id);
        if (!data) throw new Error("Invalid preferences projection");
        if (sessionGeneration.current !== generation) return;
        setRemotePrefs(old => ({ ...old, [id]: data }));
        if (data.settings.graphDisplay || data.settings.showGraphDiagnostics !== undefined) {
          setGraphViews(old => ({ ...old, [id]: sanitizedGraphView({ ...(old[id] ?? DEFAULT_GRAPH_VIEW),
            ...(data.settings.graphDisplay ? { display: data.settings.graphDisplay } : {}),
            ...(data.settings.showGraphDiagnostics !== undefined ? { showDiagnostics: data.settings.showGraphDiagnostics } : {}),
          }) }));
        }
        setPreferenceStatuses(old => ({ ...old, [id]: "Saved preferences synchronized" }));
      } catch {
        if (sessionGeneration.current === generation)
          setPreferenceStatuses(old => ({ ...old, [id]: "Remote preferences unavailable; using local view settings" }));
      } finally { preferenceInFlight.current.delete(id); }
    })();
  }, [demo, gateway, workspace?.id]);

  const updateUserPreferences = useCallback((changes: Partial<UiSettings>) => {
    if (!workspace) return;
    const id = workspace.id;
    if (demo || gateway !== "ready") return;
    const revision = remotePrefs[id]?.revision;
    if (revision === undefined) { setPreferenceStatuses(old => ({ ...old, [id]: "Load preferences before saving" })); return; }
    const settings = { ...remotePrefs[id].settings, ...changes };
    setPreferenceStatuses(old => ({ ...old, [id]: "Saving preferences…" }));
    const generation = sessionGeneration.current;
    void (async () => {
      try {
        const response = await fetch("/api/ui/preferences", { method: "PUT", credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...scopedHeaders(id) }, body: JSON.stringify({ revision, settings }) });
        if (!response.ok) throw new Error("Preferences revision conflict or service unavailable");
        const next = projectPreferences(await response.json(), id);
        if (!next) throw new Error("Invalid saved preferences");
        if (sessionGeneration.current !== generation) return;
        setRemotePrefs(old => ({ ...old, [id]: next }));
        setPreferenceStatuses(old => ({ ...old, [id]: "Saved preferences synchronized" }));
        if (next.settings.graphDisplay || next.settings.showGraphDiagnostics !== undefined) {
          setGraphViews(old => ({ ...old, [id]: sanitizedGraphView({ ...old[id],
            ...(next.settings.graphDisplay ? { display: next.settings.graphDisplay } : {}),
            ...(next.settings.showGraphDiagnostics !== undefined ? { showDiagnostics: next.settings.showGraphDiagnostics } : {}),
          }) }));
        }
      } catch { if (sessionGeneration.current === generation) {
        setPreferenceStatuses(old => ({ ...old, [id]: "Save failed or changed elsewhere; reload preferences" }));
        refreshPreferences();
      } }
    })();
  }, [demo, gateway, workspace?.id, remotePrefs, refreshPreferences]);

  const refreshTransfers = useCallback(() => {
    if (demo || gateway !== "ready" || !workspace) return;
    const id = workspace.id;
    if (transferInFlight.current.has(id)) return;
    transferInFlight.current.add(id);
    const generation = sessionGeneration.current;
    void (async () => {
      try {
        const response = await fetch("/api/ui/downloads", { cache: "no-store", credentials: "same-origin", headers: scopedHeaders(id) });
        if (response.status === 401) { if (sessionGeneration.current === generation) setGateway("login"); return; }
        if (!response.ok) throw new Error("Transfer service unavailable");
        const data = projectTransfers(await response.json(), id);
        if (!data) throw new Error("Invalid workspace transfer projection");
        if (sessionGeneration.current !== generation) return;
        setTransferFeeds(old => ({ ...old, [id]: data.items }));
        setTransferStatuses(old => ({ ...old, [id]: "Live verified-artifact progress" }));
      } catch {
        if (sessionGeneration.current === generation)
          setTransferStatuses(old => ({ ...old, [id]: "Transfer progress unavailable; retrying" }));
      } finally { transferInFlight.current.delete(id); }
    })();
  }, [demo, gateway, workspace?.id]);

  const queueTransfer = useCallback((artifact: ArtifactManifest) => {
    if (demo || gateway !== "ready" || !workspace) return;
    const id = workspace.id;
    const generation = sessionGeneration.current;
    void (async () => {
      try {
        const response = await fetch("/api/ui/downloads", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...scopedHeaders(id) },
          body: JSON.stringify({ requestId: crypto.randomUUID(), artifact, confirm: true }) });
        if (!response.ok) throw new Error("Transfer approval was rejected");
        if (sessionGeneration.current === generation) {
          setTransferStatuses(old => ({ ...old, [id]: "Pinned artifact queued; waiting for transfer worker" }));
          refreshTransfers();
        }
      } catch { if (sessionGeneration.current === generation)
        setTransferStatuses(old => ({ ...old, [id]: "Transfer request rejected; verify artifact manifest and worker" })); }
    })();
  }, [demo, gateway, workspace?.id, refreshTransfers]);

  const cancelTransfer = useCallback((transferId: string) => {
    if (demo || gateway !== "ready" || !workspace) return;
    const id = workspace.id;
    if (!/^[0-9a-f-]{36}$/i.test(transferId)) return;
    const generation = sessionGeneration.current;
    void (async () => {
      try {
        const response = await fetch(`/api/ui/downloads/${transferId}/cancel`, {
          method: "POST", credentials: "same-origin", headers: scopedHeaders(id) });
        if (!response.ok) throw new Error("Cannot cancel transfer");
        if (sessionGeneration.current === generation) refreshTransfers();
      } catch { if (sessionGeneration.current === generation)
        setTransferStatuses(old => ({ ...old, [id]: "Cancellation not accepted; check transfer state" })); }
    })();
  }, [demo, gateway, workspace?.id, refreshTransfers]);

  useEffect(() => {
    if (demo || gateway !== "ready" || !workspace) return;
    void refreshActivity();
    void refreshModels();
    void refreshPreferences();
    void refreshTransfers();
    const interval = window.setInterval(() => { if (!document.hidden) { refreshActivity(); refreshTransfers(); } }, 6000);
    const visible = () => { if (!document.hidden) refreshActivity(); };
    document.addEventListener("visibilitychange", visible);
    return () => { window.clearInterval(interval); document.removeEventListener("visibilitychange", visible); };
  }, [demo, gateway, workspace?.id, refreshActivity, refreshModels, refreshPreferences, refreshTransfers]);

  // Operator-approved application sessions. Xpra windows themselves still
  // arrive via the native codec channel and are docked independently.
  const refreshApps = useCallback(() => {
    if (demo || gateway !== "ready" || !workspace) return;
    const id = workspace.id;
    if (appsInFlight.current.has(id)) return;
    appsInFlight.current.add(id);
    const generation = sessionGeneration.current;
    void (async () => {
      try {
        const response = await fetch("/api/ui/xpra/apps", {
          credentials: "same-origin", cache: "no-store", headers: scopedHeaders(id),
        });
        if (!response.ok) throw new Error("Application operator unavailable");
        const projected = projectAppInventory(await response.json(), id);
        if (!projected) throw new Error("Untrusted application inventory");
        if (sessionGeneration.current === generation) {
          setAppInventories(prev => ({ ...prev, [id]: projected }));
          setAppStatuses(prev => ({ ...prev, [id]: projected.configured ? "Operator session ready" : "App launching not configured" }));
        }
      } catch {
        if (sessionGeneration.current === generation)
          setAppStatuses(prev => ({ ...prev, [id]: "Application launcher unavailable" }));
      } finally { appsInFlight.current.delete(id); }
    })();
  }, [demo, gateway, workspace?.id]);

  const launchApp = useCallback((kind: string) => {
    if (demo || gateway !== "ready" || !workspace || !validAppKind(kind) ||
        !appInventories[workspace.id]?.choices.some(choice => choice.kind === kind)) return;
    const id = workspace.id;
    const generation = sessionGeneration.current;
    setAppStatuses(prev => ({ ...prev, [id]: `Launching ${kind} in sandbox…` }));
    void (async () => {
      try {
        const response = await fetch("/api/ui/xpra/apps", {method:"POST", credentials:"same-origin",
          headers:{ "Content-Type":"application/json", ...scopedHeaders(id) },
          body:JSON.stringify({kind})});
        if (!response.ok) throw new Error("Launch rejected");
        if (sessionGeneration.current !== generation) return;
        setAppStatuses(prev => ({ ...prev, [id]: `${kind} launched; Xpra window discovery in progress` }));
        refreshApps();
        openPanel("applications");
      } catch {
        if (sessionGeneration.current === generation)
          setAppStatuses(prev => ({ ...prev, [id]: `${kind} launch rejected by workspace operator` }));
      }
    })();
  }, [demo, gateway, workspace?.id, appInventories, refreshApps, openPanel]);

  const appAction = useCallback((sessionId: string, action: "attach" | "detach" | "terminate") => {
    if (demo || gateway !== "ready" || !workspace || !appSessionId(sessionId) ||
        !appInventories[workspace.id]?.sessions.some(item => item.id === sessionId)) return;
    if (action === "terminate" && !window.confirm("Terminate this workspace application? Closing a pane alone never does this.")) return;
    const id = workspace.id;
    const generation = sessionGeneration.current;
    void (async () => {
      try {
        const response = await fetch(`/api/ui/xpra/apps/${sessionId}/${action}`, {
          method:"POST", credentials:"same-origin", headers:scopedHeaders(id), body:"" });
        if (!response.ok) throw new Error("Application operation rejected");
        if (sessionGeneration.current === generation) refreshApps();
      } catch {
        if (sessionGeneration.current === generation)
          setAppStatuses(prev => ({ ...prev, [id]: "Application action was not accepted" }));
      }
    })();
  }, [demo, gateway, workspace?.id, appInventories, refreshApps]);

  useEffect(() => {
    if (demo || gateway !== "ready" || !workspace) return;
    refreshApps();
    const timer = setInterval(() => { if (!document.hidden) refreshApps(); }, 4500);
    return () => clearInterval(timer);
  }, [demo, gateway, workspace?.id, refreshApps]);

  const logout = useCallback(() => {
    sessionGeneration.current++;
    for (const controller of requests.current.values()) controller.abort();
    requests.current.clear();
    void fetch("/api/ui/auth/logout", { method: "POST", credentials: "same-origin" }).then(response => {
      if (!response.ok) throw new Error("Logout not acknowledged");
      setGateway("login"); setWorkspaces([]); setMessages({}); setRunIds({}); setGraphs({});
      setChatSessions({}); setActiveChatIds({}); setChatHistoryStatus({}); chatLoadGenerations.current = {};
      setGraphHealth({}); setGraphTimelines({}); setGraphViews({}); setCompactPrefs({});
      setActivityFeeds({}); setActivityStatuses({}); setModelLists({}); setModelStatuses({});
      setRemotePrefs({}); setPreferenceStatuses({}); setTransferFeeds({}); setTransferStatuses({});
      activityCursors.current = {};
      setAppInventories({}); setAppStatuses({}); appsInFlight.current.clear();
      setXAvailable(false); setXWindowsByWorkspace({}); setXTransportByWorkspace({});
      setXErrorsByWorkspace({}); setXMinimizedByWorkspace({}); setXReconnectByWorkspace({});
      setXpraIframeSrc(null); xpraChannel.current = "";
      xSeen.current = {}; xHidden.current = {};
      setPassword(""); setSelected(0); apiRef.current = null;
    }).catch(() => setNotice("Logout failed; your existing operator session may still be active."));
  }, []);

  const session = useMemo(() => workspace ? ({ workspace, demo, isLive: !demo,
    entries: messages[workspace.id] ?? [], chatBusy: !!busy[workspace.id],
    chatSessions: chatSessions[workspace.id] ?? [], activeChatId: activeChatIds[workspace.id] ?? null,
    chatHistoryStatus: chatHistoryStatus[workspace.id] ?? "Loading chat history",
    selectChatSession, newChatSession, reconcileChatSession,
    graph: demo ? null : graphs[workspace.id] ?? null,
    graphState: demo ? "synthetic preview" : graphHealth[workspace.id] || "No active reasoning run",
    graphTimeline: demo ? null : graphTimelines[workspace.id] ?? null,
    graphView: graphViews[workspace.id] ?? { ...DEFAULT_GRAPH_VIEW },
    compact: compactPrefs[workspace.id] === true,
    setCompactPreference, updateGraphView,
    runId: runIds[workspace.id] ?? null,
    activity: activityFeeds[workspace.id] ?? null,
    activityStatus: demo ? "Preview mode · no jobs" : activityStatuses[workspace.id] || "Awaiting job activity",
    refreshActivity,
    models: modelLists[workspace.id] ?? [],
    modelStatus: demo ? "Preview mode · no catalogue" : modelStatuses[workspace.id] || "Awaiting model catalogue",
    refreshModels,
    remotePreferences: remotePrefs[workspace.id] ?? null,
    preferencesStatus: demo ? "Preview preferences are browser-local" : preferenceStatuses[workspace.id] ?? "Preferences not loaded",
    refreshPreferences, updateUserPreferences,
    transfers: transferFeeds[workspace.id] ?? [],
    transfersStatus: demo ? "Preview · no transfers" : transferStatuses[workspace.id] ?? "Transfers not loaded",
    refreshTransfers, queueTransfer, cancelTransfer,
    appInventory: appInventories[workspace.id] ?? null,
    appStatus: demo ? "Preview mode" : appStatuses[workspace.id] ?? "Loading application sessions",
    refreshApps, launchApp, appAction,
    xAvailable, xTransport: xTransportByWorkspace[workspace.id] ?? null,
    xWindows: xWindowsByWorkspace[workspace.id] ?? [],
    xWindowErrors: xErrorsByWorkspace[workspace.id] ?? {},
    xMinimizedWindows: xMinimizedByWorkspace[workspace.id] ?? [],
    xReconnectAttempts: xReconnectByWorkspace[workspace.id] ?? 0,
    retryXpra, sendXpra,
    openXWindow, closeXWindow,
    logout, sendMessage, openPanel, closePanel, dockPanel,
    floatPanel, graphFullscreen, setGraphFullscreen }) : null,
    [workspace, demo, messages, busy, chatSessions, activeChatIds, chatHistoryStatus,
      selectChatSession, newChatSession, reconcileChatSession, graphs, graphHealth, graphTimelines, graphViews,
      activityFeeds, activityStatuses, modelLists, modelStatuses, refreshActivity, refreshModels,
      remotePrefs, preferenceStatuses, transferFeeds, transferStatuses, refreshPreferences, updateUserPreferences,
      refreshTransfers, queueTransfer, cancelTransfer,
      compactPrefs, setCompactPreference, updateGraphView, runIds, logout, sendMessage, openPanel, closePanel,
      dockPanel, floatPanel, graphFullscreen, xAvailable, xTransportByWorkspace,
      appInventories, appStatuses, refreshApps, launchApp, appAction,
      xWindowsByWorkspace, xErrorsByWorkspace, xMinimizedByWorkspace,
      xReconnectByWorkspace, retryXpra, sendXpra,
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
    <MotionConfig reducedMotion={remotePrefs[workspace.id]?.settings.reducedMotion ? "always" : "user"}>
    <LayoutGroup id={`potbot-${workspace.id}`}>
    <main className="potbot-scene">
      <WorkspaceOrbit workspaces={workspaces} selected={selected} onSelect={selectWorkspace} />
      <button className="top-menu-toggle" onClick={() => openPanel("menu")}
        aria-label="Open control menu" title="Open menu">☰ <span>Menu</span></button>
      <div className="utility-rail" aria-label="Utility panes">
        {graphMinimized && <motion.button layoutId="potbot-graph-window" className="utility-orb" onClick={() => openPanel("graph")}
          aria-label="Restore reasoning graph" title="Restore thought graph">◇</motion.button>}
        {!demo && <button className="utility-small" onClick={() => openPanel("applications")}
          title="Open workspace application manager" aria-label="Manage Xpra application windows">
          ▣ {(xWindowsByWorkspace[workspace.id] ?? []).length}
        </button>}
        {!demo && (xWindowsByWorkspace[workspace.id] ?? []).slice(0, 3).map(win =>
          <button key={win.id} className="utility-small" onClick={() => openXWindow(win.id)}
            title={`Open Xpra application: ${win.title}`} aria-label={`Open Xpra window ${win.title}`}>▣</button>)}
        {!demo && <button onClick={() => openPanel("activity")} className="utility-small"
          aria-label="Open workspace job activity" title="Job activity">≋</button>}
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
    </MotionConfig>
  </WorkspaceProvider>;
}
