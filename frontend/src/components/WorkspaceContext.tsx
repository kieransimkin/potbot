"use client";

import { createContext, useContext } from "react";
import type { ChatEntry, DockEdge, PanelId, Workspace, GraphProjection } from "@/lib/model";
import type { XWindowInfo, XWindowTransport, XpraControl } from "@/lib/xpra-contract";
import type { GraphView, Timeline } from "@/lib/graph-workbench";
import type { ActivityPage, ModelListing } from "@/lib/activity";

type WorkspaceContextValue = {
  workspace: Workspace;
  demo: boolean;
  entries: ChatEntry[];
  isLive: boolean;
  chatBusy: boolean;
  graph: GraphProjection | null;
  graphState: string;
  graphTimeline: Timeline | null;
  compact: boolean;
  setCompactPreference: (enabled: boolean) => void;
  graphView: GraphView;
  updateGraphView: (next: Partial<GraphView>) => void;
  runId: string | null;
  activity: ActivityPage | null;
  activityStatus: string;
  refreshActivity: () => void;
  models: ModelListing[];
  modelStatus: string;
  refreshModels: () => void;
  xAvailable: boolean;
  xTransport: XWindowTransport | null;
  xWindows: XWindowInfo[];
  xWindowErrors: Record<string, string>;
  xMinimizedWindows: string[];
  xReconnectAttempts: number;
  retryXpra: () => void;
  openXWindow: (windowId: string) => void;
  closeXWindow: (windowId: string) => void;
  sendXpra: (windowId: string, command: XpraControl) => void;
  logout: () => void;
  sendMessage: (text: string) => void;
  openPanel: (panel: PanelId) => void;
  closePanel: (panel: PanelId) => void;
  dockPanel: (panel: PanelId, edge: DockEdge) => void;
  floatPanel: (panel: PanelId) => void;
  graphFullscreen: boolean;
  setGraphFullscreen: (value: boolean) => void;
};
const Context = createContext<WorkspaceContextValue | null>(null);
export const WorkspaceProvider = Context.Provider;
export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(Context);
  if (!value) throw new Error("Workspace panel is outside the workspace shell");
  return value;
}
