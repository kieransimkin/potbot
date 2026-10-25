/** Pure window list projection: display state belongs to Dockview, not Xpra.
 * No X11 socket paths, session tokens, or raw protocol messages enter this list.
 */
import type { XWindowInfo } from "./xpra-contract.ts";

export type WindowFilter = "all" | "visible" | "minimized";
export type WindowListItem = XWindowInfo & { minimized: boolean };

export function projectXpraWindows(
  windows: readonly XWindowInfo[], minimizedIds: readonly string[],
  query: string, filter: WindowFilter = "all",
): { items: WindowListItem[]; total: number; minimized: number; limited: boolean } {
  const hidden = new Set(minimizedIds);
  const unique = new Map<string, XWindowInfo>();
  // A late event for an old generation must not overwrite the current title.
  for (const window of windows) {
    const old = unique.get(window.id);
    if (!old || old.generation < window.generation) unique.set(window.id, window);
  }
  const all = [...unique.values()];
  const needle = query.trim().slice(0, 120).toLocaleLowerCase();
  const shown = all.filter(window => {
    const hiddenNow = hidden.has(window.id);
    if (filter === "visible" && hiddenNow) return false;
    if (filter === "minimized" && !hiddenNow) return false;
    return !needle || window.title.toLocaleLowerCase().includes(needle) || window.id.includes(needle);
  });
  shown.sort((a, b) => a.title.localeCompare(b.title) || a.id.localeCompare(b.id));
  return { items: shown.slice(0, 256).map(window => ({ ...window, minimized: hidden.has(window.id) })),
    total: all.length, minimized: all.filter(window => hidden.has(window.id)).length,
    limited: shown.length > 256 };
}
