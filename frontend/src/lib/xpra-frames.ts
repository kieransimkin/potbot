/** Zero-PNG, per-window ImageBitmap surfaces painted only into active dock panes. */
import { XWINDOW_ID } from "./xpra-contract";

const panes = new Map<string, HTMLCanvasElement>();
export function attachXpraCanvas(windowId: string, canvas: HTMLCanvasElement): () => void {
  if (!XWINDOW_ID.test(windowId)) throw new Error("Invalid Xpra window");
  panes.set(windowId, canvas);
  return () => { if (panes.get(windowId) === canvas) panes.delete(windowId); };
}
export function deliverXpraFrame(id: string, bitmap: ImageBitmap): void {
  try {
    if (!XWINDOW_ID.test(id) || bitmap.width > 1920 || bitmap.height > 1200) return;
    const canvas = panes.get(id);
    if (!canvas) return; // Window minimized; decoded frames are not retained.
    if (canvas.width !== bitmap.width) canvas.width = bitmap.width;
    if (canvas.height !== bitmap.height) canvas.height = bitmap.height;
    const render = canvas.getContext("bitmaprenderer");
    if (render) { render.transferFromImageBitmap(bitmap); return; }
    canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
  } finally { bitmap.close(); }
}
