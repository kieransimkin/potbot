/** Strict per-workspace projections: never accept local paths, secrets or raw provider metadata. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA = /^[0-9a-f]{64}$/;
const COMMIT = /^[0-9a-f]{40}$/;
const REPO = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;
const FILENAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{1,199}$/;
const QUANTS = new Set(["Q2_K", "Q3_K_M", "Q4_K_M", "Q5_K_M", "Q6_K", "Q8_0", "F16", "BF16"]);
const STATES = new Set(["queued", "downloading", "verifying", "installed", "failed", "cancelled", "indeterminate"]);
const BOOLEAN_KEYS = ["reducedMotion", "showGraphDiagnostics", "orbAutoHide", "downloadNotifications"] as const;

export type UiSettings = {
  theme?: "system" | "dark" | "light";
  graphDisplay?: "3d" | "list";
  reducedMotion?: boolean;
  showGraphDiagnostics?: boolean;
  orbAutoHide?: boolean;
  downloadNotifications?: boolean;
  xpraMaxFps?: 10 | 15 | 24 | 30 | 60;
};
export type UiPreferences = { workspaceId: string; revision: number; settings: UiSettings };
export type TransferProgress = {
  id: string; modelId: string; revision: string; filename: string; quantization: string;
  state: "queued" | "downloading" | "verifying" | "installed" | "failed" | "cancelled" | "indeterminate";
  expectedBytes: number; downloadedBytes: number; verifiedBytes: number;
  cancelRequested: boolean; errorCode: string | null; updatedAt: string;
};
export type TransferFeed = { workspaceId: string; transferTelemetry: true; installTelemetry: true; items: TransferProgress[] };
export type ArtifactManifest = {
  model_id: string; artifact_repository: string; revision: string; filename: string;
  quantization: string; sha256: string; size_bytes: number;
};

function doc(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function count(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
export function validateSettings(value: unknown): UiSettings | null {
  const body = doc(value);
  if (!body || Object.keys(body).length > 12 || Object.keys(body).some(k =>
      !["theme", "graphDisplay", ...BOOLEAN_KEYS, "xpraMaxFps"].includes(k))) return null;
  if ("theme" in body && (typeof body.theme !== "string" || !["system", "dark", "light"].includes(body.theme))) return null;
  if ("graphDisplay" in body && (typeof body.graphDisplay !== "string" || !["3d", "list"].includes(body.graphDisplay))) return null;
  if (BOOLEAN_KEYS.some(k => k in body && typeof body[k] !== "boolean")) return null;
  if ("xpraMaxFps" in body && (typeof body.xpraMaxFps !== "number" || ![10, 15, 24, 30, 60].includes(body.xpraMaxFps))) return null;
  return body as UiSettings;
}
export function projectPreferences(value: unknown, workspaceId: string): UiPreferences | null {
  const body = doc(value);
  if (!body || !UUID.test(workspaceId) || body.workspaceId !== workspaceId || !count(body.revision)) return null;
  const settings = validateSettings(body.settings);
  return settings ? { workspaceId, revision: body.revision as number, settings } : null;
}
export function validateArtifact(value: unknown): ArtifactManifest | null {
  const body = doc(value);
  if (!body || Object.keys(body).length !== 7 ||
      !["model_id", "artifact_repository", "revision", "filename", "quantization", "sha256", "size_bytes"].every(k => k in body) ||
      typeof body.model_id !== "string" || !REPO.test(body.model_id) ||
      typeof body.artifact_repository !== "string" || !REPO.test(body.artifact_repository) ||
      typeof body.revision !== "string" || !COMMIT.test(body.revision) ||
      typeof body.filename !== "string" || !FILENAME.test(body.filename) ||
      typeof body.quantization !== "string" || !QUANTS.has(body.quantization) ||
      typeof body.sha256 !== "string" || !SHA.test(body.sha256) ||
      !count(body.size_bytes) || body.size_bytes === 0 || body.size_bytes > 128 * 1024 ** 3) return null;
  if (!body.filename.endsWith(".gguf") && !(body.filename.endsWith(".safetensors") && ["BF16", "F16"].includes(body.quantization))) return null;
  return body as ArtifactManifest;
}
export function projectTransfers(value: unknown, workspaceId: string): TransferFeed | null {
  const body = doc(value);
  if (!body || !UUID.test(workspaceId) || body.workspaceId !== workspaceId || body.transferTelemetry !== true ||
      body.installTelemetry !== true || !Array.isArray(body.items) || body.items.length > 100) return null;
  const items: TransferProgress[] = [];
  for (const raw of body.items) {
    const item = doc(raw);
    if (!item || typeof item.id !== "string" || !UUID.test(item.id) ||
        typeof item.modelId !== "string" || !REPO.test(item.modelId) ||
        typeof item.revision !== "string" || !COMMIT.test(item.revision) ||
        typeof item.filename !== "string" || !FILENAME.test(item.filename) ||
        typeof item.quantization !== "string" || !QUANTS.has(item.quantization) ||
        typeof item.state !== "string" || !STATES.has(item.state) ||
        !count(item.expectedBytes) || !count(item.downloadedBytes) || !count(item.verifiedBytes) ||
        item.expectedBytes === 0 || item.downloadedBytes > item.expectedBytes || item.verifiedBytes > item.downloadedBytes ||
        typeof item.cancelRequested !== "boolean" ||
        !(item.errorCode === null || (typeof item.errorCode === "string" && item.errorCode.length <= 80)) ||
        typeof item.updatedAt !== "string" || item.updatedAt.length > 48 || !item.updatedAt.includes("T")) return null;
    items.push({ id: item.id as string, modelId: item.modelId as string,
      revision: item.revision as string, filename: item.filename as string,
      quantization: item.quantization as string, state: item.state as TransferProgress["state"],
      expectedBytes: item.expectedBytes as number, downloadedBytes: item.downloadedBytes as number,
      verifiedBytes: item.verifiedBytes as number, cancelRequested: item.cancelRequested as boolean,
      errorCode: item.errorCode as string | null, updatedAt: item.updatedAt as string });
  }
  return { workspaceId, transferTelemetry: true, installTelemetry: true, items };
}

export function bytesLabel(bytes: number): string {
  if (!count(bytes)) return "Unknown";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / (1024 ** 2)).toFixed(1)} MiB`;
  return `${(bytes / (1024 ** 3)).toFixed(2)} GiB`;
}
