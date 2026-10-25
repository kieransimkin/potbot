/** Public S11 result/approval projection. Never hydrate subprocess args, paths or credentials. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA = /^[a-f0-9]{64}$/;
const COMMIT = /^[a-f0-9]{40}$/;
const SLUG = /^[a-z][a-z0-9-]{1,62}$/;
const STATES = new Set(["awaiting_approval", "approved", "rejected", "executing", "awaiting_repin", "active", "indeterminate", "rolled_back"]);

export type UpgradeRecord = {
  id: string; workspaceId: string; slug: string; oldCommit: string; newCommit: string;
  suiteSha256: string; evidenceSha256: string; passedTests: number;
  tests: { name: string; kind: string; elapsedMs: number; passed: true }[]; revision: number;
  state: string; updatedAt: string;
};
export type ToolUpgradeListing = { schema: "potbot-tool-upgrades-s11/v1"; items: UpgradeRecord[] };

function doc(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
export function upgradeRecord(value: unknown, workspaceId: string): UpgradeRecord | null {
  const data = doc(value);
  if (!data || !UUID.test(workspaceId) || data.workspaceId !== workspaceId ||
      typeof data.id !== "string" || !UUID.test(data.id) ||
      typeof data.slug !== "string" || !SLUG.test(data.slug) ||
      typeof data.oldCommit !== "string" || !COMMIT.test(data.oldCommit) ||
      typeof data.newCommit !== "string" || !COMMIT.test(data.newCommit) ||
      data.newCommit === data.oldCommit ||
      typeof data.suiteSha256 !== "string" || !SHA.test(data.suiteSha256) ||
      typeof data.evidenceSha256 !== "string" || !SHA.test(data.evidenceSha256) ||
      !Number.isSafeInteger(data.passedTests) || Number(data.passedTests) < 2 || Number(data.passedTests) > 16 ||
      !Array.isArray(data.tests) || data.tests.length !== data.passedTests ||
      data.tests.some((raw: unknown) => {
        const item = doc(raw);
        return !item || typeof item.name !== "string" || !/^[a-z][a-z0-9_.-]{1,63}$/.test(item.name) ||
          typeof item.kind !== "string" || !["smoke", "unit", "integration", "regression"].includes(item.kind) ||
          !Number.isSafeInteger(item.elapsedMs) || Number(item.elapsedMs) < 0 || Number(item.elapsedMs) > 180000 ||
          item.passed !== true;
      }) ||
      !Number.isSafeInteger(data.revision) || Number(data.revision) < 0 ||
      typeof data.state !== "string" || !STATES.has(data.state) ||
      typeof data.updatedAt !== "string" || data.updatedAt.length > 48 || !data.updatedAt.includes("T")) return null;
  return { id: data.id, workspaceId, slug: data.slug, oldCommit: data.oldCommit, newCommit: data.newCommit,
    suiteSha256: data.suiteSha256, evidenceSha256: data.evidenceSha256,
    passedTests: Number(data.passedTests),
    tests: (data.tests as Record<string, unknown>[]).map(item => ({
      name: item.name as string, kind: item.kind as string,
      elapsedMs: item.elapsedMs as number, passed: true as const,
    })), revision: Number(data.revision),
    state: data.state, updatedAt: data.updatedAt };
}
export function upgradeListing(value: unknown, workspaceId: string): ToolUpgradeListing | null {
  const data = doc(value);
  if (!data || data.schema !== "potbot-tool-upgrades-s11/v1" || !Array.isArray(data.items) || data.items.length > 100) return null;
  const items: UpgradeRecord[] = [];
  for (const entry of data.items) {
    const parsed = upgradeRecord(entry, workspaceId);
    if (!parsed) return null;
    items.push(parsed);
  }
  return { schema: "potbot-tool-upgrades-s11/v1", items };
}
