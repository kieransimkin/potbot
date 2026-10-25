/** Damage-region ImageBitmap delivery into Dockview-owned HTMLCanvas surfaces.
 *
 * The Xpra iframe owns decoding; this module has no protocol credentials.
 * A minimized window gets no bitmap copy. A newly attached pane must receive
 * a complete frame before incremental patches can modify its backing canvas.
 */
import { XWINDOW_ID, validXpraFrame } from "./xpra-contract.ts";

type CanvasRecord = {
  surface: HTMLCanvasElement;
  generation: number;
  sequence: number;
  initialized: boolean;
  displayed: number;
  incremental: number;
  stale: number;
  pixelsCopied: number;
  framesSinceSample: number;
  sampleAt: number;
  fps: number;
  lastFrameAt: number;
  frameIntervals: number[];
  inputSamples: number[];
  pendingInputAt: number | null;
};
const panes = new Map<string, CanvasRecord>();
export type FrameDisposition = "applied" | "hidden" | "stale" | "resync" | "invalid";
export type XpraFrameStats = {
  fps: number; displayed: number; incremental: number;
  stale: number; estimatedDecodedPixelBytes: number; initialized: boolean;
  p95FrameIntervalMs: number | null;
  medianInputToNextFrameMs: number | null;
  inputSamples: number;
  lastFrameAgeMs: number | null;
};

/** Strictly a browser-side proxy for interaction latency. A subsequent frame
 * may be unrelated to this input; server-side event correlation is still needed.
 */
export function markXpraInput(id: string): void {
  const record = panes.get(id);
  if (record && record.initialized && record.pendingInputAt === null)
    record.pendingInputAt = performance.now();
}

function percentile(values: number[], quantile: number): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return Math.round(sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * quantile) - 1)]);
}


export function attachXpraCanvas(id: string, surface: HTMLCanvasElement, generation: number): () => void {
  if (!XWINDOW_ID.test(id) || !Number.isSafeInteger(generation) || generation < 1)
    throw new Error("Invalid Xpra window generation");
  const record: CanvasRecord = {
    surface, generation, sequence: 0, initialized: false,
    displayed: 0, incremental: 0, stale: 0, pixelsCopied: 0,
    framesSinceSample: 0, sampleAt: performance.now(), fps: 0, lastFrameAt: 0,
    frameIntervals: [], inputSamples: [], pendingInputAt: null,
  };
  panes.set(id, record);
  return () => { if (panes.get(id) === record) panes.delete(id); };
}

export function deliverXpraFrame(id: string, bitmap: ImageBitmap, payload: unknown): FrameDisposition {
  try {
    const meta = validXpraFrame(payload, bitmap);
    if (!meta || id !== meta.id) return "invalid";
    const record = panes.get(id);
    if (!record) return "hidden";
    if (meta.generation !== record.generation || meta.sequence <= record.sequence) {
      record.stale++;
      return "stale";
    }
    if (!meta.full && (!record.initialized || meta.sequence !== record.sequence + 1 ||
        record.surface.width !== meta.surfaceWidth || record.surface.height !== meta.surfaceHeight)) {
      // Never patch an uninitialized, freshly resized or restored canvas.
      record.stale++;
      return "resync";
    }
    const ctx = record.surface.getContext("2d", { alpha: false, desynchronized: true });
    if (!ctx) return "invalid";
    if (meta.full) {
      if (record.surface.width !== meta.surfaceWidth) record.surface.width = meta.surfaceWidth;
      if (record.surface.height !== meta.surfaceHeight) record.surface.height = meta.surfaceHeight;
      ctx.drawImage(bitmap, 0, 0);
      record.initialized = true;
    } else {
      ctx.drawImage(bitmap, meta.x, meta.y);
      record.incremental++;
    }
    record.sequence = meta.sequence;
    record.displayed++;
    record.pixelsCopied += meta.width * meta.height * 4;
    const now = performance.now();
    if (record.lastFrameAt > 0) {
      const gap = now - record.lastFrameAt;
      // Suspended tabs may spend minutes asleep. Don't count that as frame jank.
      if (gap >= 0 && gap <= 5000) {
        record.frameIntervals.push(gap);
        if (record.frameIntervals.length > 120) record.frameIntervals.shift();
      }
    }
    if (record.pendingInputAt !== null) {
      const latency = now - record.pendingInputAt;
      if (latency >= 0 && latency <= 5000) {
        record.inputSamples.push(latency);
        if (record.inputSamples.length > 90) record.inputSamples.shift();
      }
      record.pendingInputAt = null;
    }
    record.lastFrameAt = now;
    record.framesSinceSample++;
    const elapsed = performance.now() - record.sampleAt;
    if (elapsed >= 1000) {
      record.fps = Math.round(record.framesSinceSample * 1000 / elapsed);
      record.framesSinceSample = 0;
      record.sampleAt = performance.now();
    }
    return "applied";
  } catch {
    return "invalid";
  } finally {
    // The transfer from the decoder iframe gives us ownership; release after
    // copying to our persistent canvas, including all drop/error paths.
    bitmap.close();
  }
}

export function xpraFrameStats(id: string): XpraFrameStats | null {
  const s = panes.get(id);
  if (!s) return null;
  return { fps: s.lastFrameAt && performance.now() - s.lastFrameAt < 2000 ? s.fps : 0,
    displayed: s.displayed, incremental: s.incremental,
    stale: s.stale, estimatedDecodedPixelBytes: s.pixelsCopied,
    initialized: s.initialized,
    p95FrameIntervalMs: percentile(s.frameIntervals, .95),
    medianInputToNextFrameMs: percentile(s.inputSamples, .5),
    inputSamples: s.inputSamples.length,
    lastFrameAgeMs: s.lastFrameAt ? Math.max(0, Math.round(performance.now() - s.lastFrameAt)) : null,
  };
}
