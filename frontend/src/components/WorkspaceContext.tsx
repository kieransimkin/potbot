"use client";

import { createContext, useContext } from "react";
import type { ChatEntry, DockEdge, PanelId, Workspace } from "@/lib/model";

type WorkspaceContextValue = {
  workspace: Workspace;
  demo: boolean;
  entries: ChatEntry[];
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
