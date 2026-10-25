"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { DockviewApi, DockviewReadyEvent } from "dockview-react";
import { DockviewReact } from "dockview-react";
import { AnimatePresence, LayoutGroup, motion } from "motion/react";
import { bootWorkspaces } from "@/lib/boot";
import { decodeSavedLayout, layoutStorageKey, PANEL_TITLES, shouldMinimizePanel } from "@/lib/layout";
import type { ChatEntry, DockEdge, PanelId, Workspace } from "@/lib/model";
import { WorkspaceOrbit } from "./WorkspaceOrbit";
import { WorkspaceProvider } from "./WorkspaceContext";
import { BrowserPanel, ChatPanel, DownloadsPanel, FullscreenGraph, GraphPanel, InstallationsPanel, MenuPanel, PreferencesPanel, TerminalPanel } from "./Panels";

const COMPONENTS = {
  chat: ChatPanel, graph: GraphPanel, menu: MenuPanel, terminal: TerminalPanel,
  browser: BrowserPanel, preferences: PreferencesPanel, downloads: DownloadsPanel,
  installations: InstallationsPanel,
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

function DockedScene({ workspace, onGraphMinimized }: {
  workspace: Workspace;
  onGraphMinimized: (value: boolean) => void;
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
      });
    });
    const maximized = api.onDidMaximizedGroupChange(() => {
      // Menu and conversation must not occupy the entire scene.
      if (api.getPanel("menu")?.api.isMaximized() || api.getPanel("chat")?.api.isMaximized()) api.exitMaximizedGroup();
    });
    onGraphMinimized(!api.getPanel("graph"));
    // Cleanup is handled when workspace-specific Dockview unmounts.
    cleanupRef.current = () => { clearTimeout(debounce); persist(); changed.dispose(); removed.dispose(); maximized.dispose(); };
  }, [apiRef, workspace.id, onGraphMinimized]);
  const cleanupRef = useRef<() => void>(() => {});
  useEffect(() => () => cleanupRef.current(), []);
  return <DockviewReact className="dockview-theme-dark potbot-dockview" onReady={onReady}
    components={COMPONENTS} floatingGroupBounds="boundedWithinViewport"
    floatingGroupDragHandle="titlebar"
    tabAnimation="smooth" keyboardNavigation />;
}

// Shared across panel commands and this workspace's Dockview instance.
const DockApiContext = createContext<React.MutableRefObject<DockviewApi | null> | null>(null);
function useDockApi() {
  const ref = useContext(DockApiContext);
  if (!ref) throw new Error("Docking panel is outside the workspace layout");
  return ref;
}

export function OrbWorkspace({ demo }: { demo: boolean }) {
  const { workspaces, source } = useMemo(() => bootWorkspaces(demo), [demo]);
  const [selected, setSelected] = useState(0);
  const [messages, setMessages] = useState<Record<string, ChatEntry[]>>({});
  const [graphMinimized, setGraphMinimized] = useState(false);
  const [graphFullscreen, setGraphFullscreen] = useState(false);
  const apiRef = useRef<DockviewApi | null>(null);
  const workspace = workspaces[selected];

  useEffect(() => {
    if (!demo) return;
    try {
      const last = localStorage.getItem("potbot:ui:selected-workspace:v1");
      const index = workspaces.findIndex(item => item.id === last);
      if (index >= 0) setSelected(index);
    } catch { /* Storage is optional. */ }
  }, [demo, workspaces]);

  const selectWorkspace = useCallback((index: number) => {
    if (index < 0 || index >= workspaces.length) return;
    setGraphFullscreen(false);
    apiRef.current = null;
    setSelected(index);
    try { localStorage.setItem("potbot:ui:selected-workspace:v1", workspaces[index].id); } catch {}
  }, [workspaces]);

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
    if (!workspace || !demo) return;
    const id = crypto.randomUUID();
    setMessages(previous => ({ ...previous, [workspace.id]: [
      ...(previous[workspace.id] ?? []),
      { id, role: "user", text },
      { id: crypto.randomUUID(), role: "status", text: "UI preview only — this message was not sent to the Potbot backend." },
    ] }));
  }, [workspace, demo]);
  const session = useMemo(() => workspace ? ({ workspace, demo,
    entries: messages[workspace.id] ?? [], sendMessage, openPanel, closePanel, dockPanel,
    floatPanel, graphFullscreen, setGraphFullscreen }) : null,
    [workspace, demo, messages, sendMessage, openPanel, closePanel, dockPanel, floatPanel, graphFullscreen]);

  if (!workspace || !session) return <main className="setup-screen">
    <span className="setup-icon">✳</span><h1>Potbot Orb Workspace</h1>
    <p>The UI shell is ready. Workspace discovery and authenticated sessions still need the backend endpoints from the integration contract.</p>
    <span className="status-chip">{source === "unconfigured" ? "BACKEND NOT CONNECTED" : "NO WORKSPACES"}</span>
    {process.env.NODE_ENV !== "production" && <a className="demo-button" href="/?demo=1">Open interactive demo →</a>}
    <small>No synthetic workspace is shown as a real backend resource.</small>
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
        <button onClick={() => openPanel("preferences")} className="utility-small" aria-label="Preferences" title="Preferences">⚙</button>
      </div>
      <div className="scene-presence"><span className="presence-light" />{demo ? "INTERACTIVE PREVIEW" : "LIVE"}</div>
      <DockApiContext.Provider value={apiRef}>
        <div className="dock-root" key={workspace.id}>
          <DockedScene workspace={workspace} onGraphMinimized={setGraphMinimized} />
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
