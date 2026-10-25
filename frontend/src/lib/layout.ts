import type { PanelId, UtilityPanel } from "./model";

export const PANEL_TITLES: Record<PanelId, string> = {
  chat: "Conversation", menu: "Control menu", graph: "Thought graph",
  terminal: "Workspace Bash", browser: "Controlled browser",
  preferences: "Preferences", downloads: "Downloads", installations: "Model installations",
  applications: "Xpra applications", activity: "Workspace activity",
};

export const RESTORABLE_PANELS: UtilityPanel[] = [
  "menu", "graph", "terminal", "browser", "preferences", "downloads", "installations", "applications", "activity",
];

export type SavedLayout = {
  version: 1; workspaceId: string; dock: unknown;
  graphMinimized: boolean; savedAt: number;
};

export function layoutStorageKey(workspaceId: string): string {
  if (!workspaceId || workspaceId.length > 128 || !/^[a-zA-Z0-9_-]+$/.test(workspaceId)) throw new Error("Invalid workspace ID");
  return `potbot:ui:layout:v1:${workspaceId}`;
}

export function decodeSavedLayout(raw: string | null, workspaceId: string): SavedLayout | null {
  if (!raw || raw.length > 200_000) return null;
  try {
    const data: unknown = JSON.parse(raw);
    if (!data || typeof data !== "object") return null;
    const value = data as Record<string, unknown>;
    if (value.version !== 1 || value.workspaceId !== workspaceId || typeof value.savedAt !== "number" || !Number.isFinite(value.savedAt)) return null;
    if (!value.dock || typeof value.dock !== "object" || Array.isArray(value.dock)) return null;
    return { version: 1, workspaceId, dock: value.dock, graphMinimized: value.graphMinimized === true, savedAt: value.savedAt };
  } catch { return null; }
}

export function shouldMinimizePanel(panelId: string): panelId is UtilityPanel {
  return RESTORABLE_PANELS.includes(panelId as UtilityPanel);
}
