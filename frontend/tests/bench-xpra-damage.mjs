/** CPU-side diagnostic for browser frame-metadata validation and copy volume.
 * Not an actual browser/GPU frame-rate benchmark; no Xpra network traffic.
 */
import { performance } from 'node:perf_hooks';
import { validXpraFrame } from '../src/lib/xpra-contract.ts';

const WINDOW_COUNT = 8, FPS = 30, SECONDS = 5;
const fullW = 1280, fullH = 720;
const patchW = 48, patchH = 48;
const frames = WINDOW_COUNT * FPS * SECONDS;
const started = performance.now();
let accepted = 0;
for (let i = 0; i < frames; i++) {
  const meta = { id: `xwin-${(i % WINDOW_COUNT + 1).toString(16)}`,
    generation: 1, sequence: i + 1, full: false,
    x: (i * 37) % (fullW - patchW), y: (i * 21) % (fullH - patchH),
    width: patchW, height: patchH, surfaceWidth: fullW, surfaceHeight: fullH };
  if (validXpraFrame(meta, { width: patchW, height: patchH })) accepted++;
}
const elapsed = performance.now() - started;
if (accepted !== frames) throw new Error('Invalid Xpra damage-region projection');
const fullBytes = frames * fullW * fullH * 4;
const patchBytes = frames * patchW * patchH * 4;
console.log(JSON.stringify({
  benchmark: 'synthetic-xpra-damage-metadata-v1',
  windows: WINDOW_COUNT, fpsPerWindowAssumed: FPS, secondsAssumed: SECONDS,
  frames, validated: accepted,
  metadataValidationMs: Number(elapsed.toFixed(2)),
  estimatedFullCanvasCopyMiB: Number((fullBytes / 2**20).toFixed(1)),
  estimatedCroppedCopyMiB: Number((patchBytes / 2**20).toFixed(1)),
  estimatedPixelCopyReduction: Number((1 - patchBytes / fullBytes).toFixed(4)),
  measuredGpuFps: null, measuredNetworkBandwidth: null,
  note: 'Synthetic copy volume for 48x48 damage per 1280x720 window. Not a GPU or encoded-network benchmark.',
}, null, 2));
