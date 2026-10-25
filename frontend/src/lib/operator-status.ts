const EPOCH = /^[0-9a-f]{32}$/;
const SCHEMA = "potbot-ui-operator/v1";

type OperatorView = {
  schema: typeof SCHEMA; workspaceId: string; configured: boolean; healthy: boolean;
  journal: "healthy" | "unavailable"; releaseGate: "observed" | "unknown";
  pendingReleases: number | null; activeRuns: number | null; epoch: string | null;
};

export function projectOperatorStatus(raw: unknown, workspaceId: string): OperatorView | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  if (r.schema !== SCHEMA || r.workspaceId !== workspaceId ||
      typeof r.configured !== "boolean" || typeof r.healthy !== "boolean" ||
      !["healthy", "unavailable"].includes(String(r.journal)) ||
      !["observed", "unknown"].includes(String(r.releaseGate)) ||
      !(r.pendingReleases === null || (Number.isSafeInteger(r.pendingReleases) &&
        (r.pendingReleases as number) >= 0 && (r.pendingReleases as number) <= 100000)) ||
      !(r.activeRuns === null || (Number.isSafeInteger(r.activeRuns) &&
        (r.activeRuns as number) >= 0 && (r.activeRuns as number) <= 100000)) ||
      !(r.epoch === null || (typeof r.epoch === "string" && EPOCH.test(r.epoch)))) return null;
  if (r.healthy === true && (r.configured !== true || r.journal !== "healthy" ||
      typeof r.epoch !== "string" || r.activeRuns === null)) return null;
  return { schema: SCHEMA, workspaceId, configured: r.configured as boolean,
    healthy: r.healthy as boolean, journal: r.journal as OperatorView["journal"],
    releaseGate: r.releaseGate as OperatorView["releaseGate"],
    pendingReleases: r.pendingReleases as number | null, activeRuns: r.activeRuns as number | null,
    epoch: r.epoch as string | null };
}
