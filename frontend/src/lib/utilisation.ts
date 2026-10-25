/** Strict read-only projection of operator telemetry. Never accept raw audit paths. */
export type AgentHeat = { agentId: string; heat: number };
export type SystemUtilisation = {
  schema: 1; available: boolean;
  system: { load3m: number; load2s: number; memoryUsed: number;
    logicalCpus: number; pressure: Record<"cpu" | "memory" | "io", Record<string, number>> };
  agents: AgentHeat[];
};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const object = (v: unknown): Record<string, unknown> | null =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : null;
const fraction = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 1;

export function projectUtilisation(value: unknown): SystemUtilisation | null {
  const root = object(value), system = object(root?.system);
  if (root?.schema !== 1 || typeof root.available !== "boolean" || !system ||
      !fraction(system.load3m) || !fraction(system.load2s) || !fraction(system.memoryUsed) ||
      !Number.isSafeInteger(system.logicalCpus) || (system.logicalCpus as number) < 1 ||
      (system.logicalCpus as number) > 65536 || !Array.isArray(root.agents) || root.agents.length > 128) return null;
  const pressure = object(system.pressure);
  if (!pressure) return null;
  const filtered: SystemUtilisation["system"]["pressure"] = { cpu: {}, memory: {}, io: {} };
  for (const resource of ["cpu", "memory", "io"] as const) {
    const group = object(pressure[resource]);
    if (!group) return null;
    for (const k of ["avg10", "avg60", "avg300"] as const) {
      const v = group[k];
      if (v !== undefined) {
        if (typeof v !== "number" || !Number.isFinite(v) || v < 0 || v > 100) return null;
        filtered[resource][k] = v;
      }
    }
  }
  const agents: AgentHeat[] = [], seen = new Set<string>();
  for (const raw of root.agents) {
    const a = object(raw);
    if (!a || typeof a.agentId !== "string" || !UUID.test(a.agentId) ||
        !fraction(a.heat) || seen.has(a.agentId)) return null;
    seen.add(a.agentId);
    agents.push({ agentId: a.agentId, heat: a.heat });
  }
  return { schema: 1, available: root.available,
    system: { load3m: system.load3m, load2s: system.load2s,
      memoryUsed: system.memoryUsed, logicalCpus: system.logicalCpus as number, pressure: filtered },
    agents };
}
