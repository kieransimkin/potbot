/** Scoped Xpra window/frame registry: no agent input, host PIDs, or bearer tokens. */
import type { DockviewApi } from "dockview-react";
import { validXpraFrame } from "./xpra-contract.ts";
import { agentWindowKey, scopedAgentWindow } from "./agent-xpra-contract.ts";
import type { AgentWindowInfo, AgentXpraSession } from "./agent-xpra-contract.ts";

type Canvas = { surface: HTMLCanvasElement; epoch: number; generation: number;
  sequence: number; initialized: boolean; frames: number };
type Publisher = (id: string, action: "frame" | "visibility", visible?: boolean) => void;
export type AgentXpraSnapshot = { sessions: readonly AgentXpraSession[];
  windows: readonly AgentWindowInfo[]; status: string };

export class AgentXpraHub {
  readonly workspaceId: string;
  private windows = new Map<string, AgentWindowInfo>();
  private canvases = new Map<string, Canvas>();
  private streams = new Map<string, { publisher: Publisher; epoch: number }>();
  // Monotonic across disconnects, even before the first X window appears.
  private epochs = new Map<string, number>();
  private listeners = new Set<() => void>();
  private dock: (() => DockviewApi | null) | null = null;
  private hidden = new Set<string>();
  private view: AgentXpraSnapshot = {sessions: [], windows: [], status: "Discovering private agent displays"};

  constructor(workspaceId: string) { this.workspaceId = workspaceId; }
  subscribe = (fn: () => void): (() => void) => { this.listeners.add(fn); return () => this.listeners.delete(fn); };
  snapshot = (): AgentXpraSnapshot => this.view;
  private changed(): void {
    this.view = {...this.view, windows: [...this.windows.values()]};
    for (const fn of this.listeners) fn();
  }
  setDock(get: () => DockviewApi | null): void { this.dock = get; }
  setStatus(status: string): void { if (this.view.status !== status) { this.view = {...this.view, status}; this.changed(); } }
  setSessions(sessions: AgentXpraSession[]): void {
    const oldBySession = new Map(this.view.sessions.map(x => [x.id, x.agentId]));
    for (const replacement of sessions) {
      if (oldBySession.has(replacement.id) && oldBySession.get(replacement.id) !== replacement.agentId)
        this.removeSession(replacement.id); // A browser can never transfer live profile ownership.
    }
    const live = new Set(sessions.map(x => x.id));
    for (const sessionId of new Set([...this.windows.values()].map(w => w.sessionId))) {
      if (!live.has(sessionId)) this.removeSession(sessionId);
    }
    this.view = {...this.view, sessions: [...sessions]};
    this.changed();
  }
  private removeKey(key: string): void {
    this.canvases.delete(key);
    if (!this.windows.delete(key)) return;
    const panel = this.dock?.()?.getPanel(key);
    if (panel) this.dock?.()?.removePanel(panel);
    this.hidden.delete(key);
  }
  removeSession(sessionId: string): void {
    for (const [key, w] of this.windows) if (w.sessionId === sessionId) this.removeKey(key);
    this.streams.delete(sessionId);
    this.epochs.delete(sessionId);
    this.changed();
  }
  disconnected(sessionId: string, epoch?: number): void {
    // A stale iframe must never disconnect a newer connection for this session.
    if (epoch !== undefined && this.streams.get(sessionId)?.epoch !== epoch) return;
    this.streams.delete(sessionId);
    for (const [key, window] of this.windows) if (window.sessionId === sessionId)
      this.windows.set(key, {...window, connected: false});
    this.changed();
  }
  attachedStream(sessionId: string, publisher: Publisher): number {
    if (!this.view.sessions.some(s => s.id === sessionId))
      throw Error("Agent stream is no longer present in this workspace");
    const epoch = (this.epochs.get(sessionId) ?? 0) + 1;
    this.epochs.set(sessionId, epoch);
    this.streams.set(sessionId, {publisher, epoch});
    return epoch;
  }
  observe(session: AgentXpraSession, raw: unknown, epoch: number): void {
    if (this.streams.get(session.id)?.epoch !== epoch ||
        !this.view.sessions.some(s => s.id === session.id && s.agentId === session.agentId)) return;
    const incoming = scopedAgentWindow(this.workspaceId, session, raw, epoch);
    if (!incoming) return;
    const prior = this.windows.get(incoming.key);
    if (!prior && (this.windows.size >= 128 ||
        [...this.windows.values()].filter(w => w.sessionId === session.id).length >= 24)) return;
    this.windows.set(incoming.key, incoming);
    // An iframe reconnect may restart native generations at 1: the separate
    // stream epoch invalidates old canvas frames even when numeric XID repeats.
    if (!prior || prior.epoch !== epoch || prior.generation !== incoming.generation)
      this.canvases.delete(incoming.key);
    this.openWindow(incoming.key, true);
    this.changed();
  }
  openWindow(key: string, automatic = false): void {
    const w = this.windows.get(key);
    if (!w || (automatic && this.hidden.has(key))) return;
    if (!automatic) this.hidden.delete(key);
    const api = this.dock?.();
    if (!api) return;
    if (api.getPanel(key)) { if (!automatic) api.getPanel(key)?.api.setActive(); return; }
    api.addPanel({id: key, component: "agent-xwindow", title: `Agent ${w.agentId.slice(0, 8)} · ${w.title}`,
      params: {agentWindowKey: key}, minimumWidth: 240, minimumHeight: 185,
      floating: {position: {top: 155, right: 45}, width: Math.min(w.width + 60, 720),
        height: Math.min(w.height + 90, 560)}});
  }
  manuallyHidden(key: string): void { if (this.windows.has(key)) this.hidden.add(key); }
  openActiveWindows(): void {
    for (const key of this.windows.keys()) this.openWindow(key, true);
  }
  closed(sessionId: string, rawId: string, generation: number, epoch: number): void {
    const key = agentWindowKey(this.workspaceId, sessionId, rawId);
    const w = this.windows.get(key);
    if (!w || w.generation !== generation || w.epoch !== epoch) return;
    this.removeKey(key);
    this.changed();
  }
  attachCanvas(key: string, surface: HTMLCanvasElement): () => void {
    const w = this.windows.get(key);
    if (!w) return () => {};
    const record: Canvas = {surface, epoch:w.epoch, generation:w.generation,
      sequence:0, initialized:false, frames:0};
    this.canvases.set(key, record);
    this.send(w.sessionId, w.id, "visibility", true);
    this.send(w.sessionId, w.id, "frame");
    return () => {
      // Late React cleanups must not hide a newly mounted canvas on the same stream.
      if (this.canvases.get(key) !== record) return;
      this.canvases.delete(key);
      this.send(w.sessionId, w.id, "visibility", false);
    };
  }
  send(sessionId: string, winId: string, action: "frame" | "visibility", visible?: boolean): void {
    this.streams.get(sessionId)?.publisher(winId, action, visible);
  }
  deliver(sessionId: string, payload: unknown, bitmap: ImageBitmap, epoch: number): void {
    try {
      // A disconnected decoder cannot paint after its stream grant is revoked.
      if (this.streams.get(sessionId)?.epoch !== epoch) return;
      if (!payload || typeof payload !== "object" || !('id' in payload) ||
          typeof payload.id !== "string") return;
      const key = agentWindowKey(this.workspaceId, sessionId, payload.id);
      const window = this.windows.get(key);
      if (!window || window.epoch !== epoch) return;
      const meta = validXpraFrame(payload, bitmap);
      if (!meta || meta.generation !== window.generation) return;
      const canvas = this.canvases.get(key);
      if (!canvas || canvas.epoch !== epoch || canvas.generation !== meta.generation) return;
      if (meta.sequence <= canvas.sequence) return;
      if (!meta.full && (!canvas.initialized || meta.sequence !== canvas.sequence + 1 ||
          canvas.surface.width !== meta.surfaceWidth || canvas.surface.height !== meta.surfaceHeight)) {
        this.send(sessionId, meta.id, "frame");
        return;
      }
      const ctx = canvas.surface.getContext("2d", {alpha: false, desynchronized: true});
      if (!ctx) return;
      if (meta.full) {
        canvas.surface.width = meta.surfaceWidth;
        canvas.surface.height = meta.surfaceHeight;
        ctx.drawImage(bitmap, 0, 0);
        canvas.initialized = true;
      } else ctx.drawImage(bitmap, meta.x, meta.y);
      canvas.sequence = meta.sequence;
      canvas.frames++;
    } catch { /* Untrusted remote frame: discard, no fallback. */ }
    finally { bitmap.close(); }
  }
  reset(): void {
    for (const key of [...this.windows.keys()]) this.removeKey(key);
    this.streams.clear(); this.epochs.clear(); this.canvases.clear(); this.hidden.clear(); this.dock = null;
    this.view = {sessions:[],windows:[],status:"Private agent displays disconnected"};
    for (const fn of this.listeners) fn();
  }
}
