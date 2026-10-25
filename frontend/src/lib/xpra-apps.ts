/** Only allowlisted application metadata may cross from the Xpra operator relay. */
export type AppChoice = { kind: string; title: string };
export type AppSession = { id: string; kind: string; title: string;
  state: "running" | "exited" | "indeterminate"; detached: boolean;
  startedAt: number; exitCode: number | null };
export type AppInventory = { workspaceId: string; configured: boolean;
  choices: AppChoice[]; sessions: AppSession[]; xpraReadOnly?: boolean };

const KIND = /^[a-z][a-z0-9-]{1,31}$/;
const ID = /^[0-9a-f]{32}$/;
export function appSessionId(value: string): boolean { return ID.test(value); }
export function validAppKind(value: unknown): value is string {
  return typeof value === "string" && KIND.test(value);
}
export function projectAppSession(value: unknown): AppSession | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const x = value as Record<string, unknown>;
  if (typeof x.id !== "string" || !ID.test(x.id) || !validAppKind(x.kind) ||
      typeof x.title !== "string" || !x.title || x.title.length > 80 ||
      !["running", "exited", "indeterminate"].includes(String(x.state)) ||
      typeof x.detached !== "boolean" || !Number.isSafeInteger(x.startedAt) ||
      (x.startedAt as number) < 0 ||
      !(x.exitCode === null || (Number.isInteger(x.exitCode) &&
        (x.exitCode as number) >= -255 && (x.exitCode as number) <= 255))) return null;
  return { id: x.id, kind: x.kind, title: x.title,
    state: x.state as AppSession["state"], detached: x.detached,
    startedAt: x.startedAt as number, exitCode: x.exitCode as number | null };
}
export function projectAppInventory(value: unknown, workspaceId: string): AppInventory | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const x = value as Record<string, unknown>;
  if (x.workspaceId !== workspaceId || typeof x.configured !== "boolean" ||
      !Array.isArray(x.choices) || x.choices.length > 12 ||
      !Array.isArray(x.sessions) || x.sessions.length > 100) return null;
  const choices: AppChoice[] = [];
  const sessions: AppSession[] = [];
  for (const item of x.choices) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const row = item as Record<string, unknown>;
    if (!validAppKind(row.kind) || typeof row.title !== "string" ||
        !row.title || row.title.length > 80 || choices.some(c => c.kind === row.kind)) return null;
    choices.push({ kind: row.kind, title: row.title });
  }
  for (const item of x.sessions) {
    const session = projectAppSession(item);
    if (!session || sessions.some(s => s.id === session.id)) return null;
    sessions.push(session);
  }
  if (!x.configured && (choices.length || sessions.length)) return null;
  if (x.xpraReadOnly !== undefined && typeof x.xpraReadOnly !== "boolean") return null;
  return { workspaceId, configured: x.configured, choices, sessions,
    ...(typeof x.xpraReadOnly === "boolean" ? {xpraReadOnly:x.xpraReadOnly} : {}) };
}
