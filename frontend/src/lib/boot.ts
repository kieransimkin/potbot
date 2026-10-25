import type { Workspace } from "./model";
import { DEMO_WORKSPACES } from "./model";

/** No unconfigured production frontend fabricates real workspaces. */
export function bootWorkspaces(demo: boolean): { workspaces: Workspace[]; source: "demo" | "unconfigured" } {
  return demo ? { workspaces: DEMO_WORKSPACES, source: "demo" } : { workspaces: [], source: "unconfigured" };
}
