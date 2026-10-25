/** Strict read-only workspace activity and model-catalog projections.
 * No job input/result, provider credentials, raw event payloads or paths are returned.
 */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STATE = new Set(["queued", "running", "cancelling", "succeeded", "failed", "cancelled", "indeterminate"]);
const NAME = /^[A-Za-z][A-Za-z0-9_.:-]{0,95}$/;
const MODEL = /^[A-Za-z0-9][A-Za-z0-9_.\/:@+\-]{0,249}$/;
const MAX_EVENTS = 100;

export type JobSummary = { id: string; taskId: string; type: string; state: string;
  stage: string | null; completed: number | null; total: number | null;
  createdAt: string | null; updatedAt: string | null; cancellationRequested: boolean };
export type ActivityEvent = { sequence: number; jobId: string; type: string; recordedAt: string | null };
export type ActivityPage = { jobs: JobSummary[]; events: ActivityEvent[]; cursor: number;
  hasMore: boolean; gap: boolean; transferTelemetry: false; installTelemetry: false };
export type ModelListing = { id: string; role: string; qualified: boolean; configured: boolean;
  responseProtocol: string | null };

function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function bounded(value: unknown, limit = 160): string | null {
  return typeof value === "string" && value.length <= limit ? value : null;
}
function stamp(value: unknown): string | null {
  return typeof value === "string" && value.length >= 10 && value.length <= 48 &&
    /^\d{4}-\d{2}-\d{2}T/.test(value) ? value : null;
}
function count(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}
function unwrap(value: unknown): Record<string, unknown> | null {
  const body = object(value);
  if (!body || body.schema_version !== 1 || body.error !== null) return null;
  return object(body.data);
}

export function projectJobs(value: unknown): JobSummary[] | null {
  const doc = unwrap(value);
  if (!doc || !Array.isArray(doc.items) || doc.items.length > 50) return null;
  const jobs: JobSummary[] = [];
  for (const item of doc.items) {
    const row = object(item);
    if (!row || typeof row.id !== "string" || !UUID.test(row.id) ||
        typeof row.episode_id !== "string" || !UUID.test(row.episode_id) ||
        typeof row.job_type !== "string" || !NAME.test(row.job_type) ||
        typeof row.state !== "string" || !STATE.has(row.state)) return null;
    const p = object(row.progress);
    const completed = p ? count(p.completed) : null;
    const total = p ? count(p.total) : null;
    jobs.push({ id: row.id, taskId: row.episode_id, type: row.job_type, state: row.state,
      stage: p && typeof p.stage === "string" && NAME.test(p.stage) ? p.stage : null,
      completed: completed !== null && total !== null && completed <= total ? completed : null,
      total: completed !== null && total !== null && completed <= total ? total : null,
      createdAt: stamp(row.created_at), updatedAt: stamp(row.updated_at),
      cancellationRequested: row.cancellation_requested === true });
  }
  return jobs;
}

export function projectJobEvents(value: unknown, after: number): Pick<ActivityPage, "events" | "cursor" | "gap" | "hasMore"> | null {
  if (!Number.isSafeInteger(after) || after < 0) return null;
  const doc = unwrap(value);
  if (!doc || !Array.isArray(doc.items) || doc.items.length > MAX_EVENTS ||
      count(doc.next_cursor) === null || typeof doc.has_more !== "boolean") return null;
  const events: ActivityEvent[] = [];
  let cursor = after, gap = false;
  for (const raw of doc.items) {
    const row = object(raw);
    if (!row || typeof row.job_id !== "string" || !UUID.test(row.job_id) ||
        typeof row.event_type !== "string" || !NAME.test(row.event_type) ||
        count(row.sequence) === null || (row.sequence as number) <= cursor) return null;
    const next = row.sequence as number;
    if (next !== cursor + 1) gap = true;
    events.push({ sequence: next, jobId: row.job_id, type: row.event_type,
      recordedAt: stamp(row.recorded_at) });
    cursor = next;
  }
  if (doc.next_cursor !== cursor) return null;
  return { events, cursor, gap, hasMore: doc.has_more };
}

export function projectModels(value: unknown): ModelListing[] | null {
  const doc = unwrap(value);
  if (!doc || !Array.isArray(doc.local) || doc.local.length > 120) return null;
  const result: ModelListing[] = [];
  for (const item of doc.local) {
    const model = object(item);
    if (!model || typeof model.canonical_model !== "string" || !MODEL.test(model.canonical_model)) return null;
    const role = bounded(model.role, 48) ?? "unspecified";
    result.push({ id: model.canonical_model, role,
      qualified: model.qualified === true, configured: model.deployment_configured === true,
      responseProtocol: bounded(model.response_protocol, 80) });
  }
  return result;
}

export function activityView(jobs: JobSummary[], eventPage: ReturnType<typeof projectJobEvents>): ActivityPage | null {
  if (!eventPage) return null;
  return { jobs, ...eventPage, transferTelemetry: false, installTelemetry: false };
}

export type JobViewFilter = "all" | "active" | "finished";
const ACTIVE_STATES = new Set(["queued", "running", "cancelling"]);
/** Pure, bounded filtering; never changes or hides authoritative job state. */
export function filterJobs(jobs: JobSummary[], query: string, state: JobViewFilter): JobSummary[] {
  const needle = query.trim().slice(0, 120).toLocaleLowerCase();
  return jobs.filter(job =>
    (state === "all" || (state === "active" ? ACTIVE_STATES.has(job.state) : !ACTIVE_STATES.has(job.state))) &&
    (!needle || job.type.toLocaleLowerCase().includes(needle) ||
     (job.stage || "").toLocaleLowerCase().includes(needle) || job.id.toLocaleLowerCase().includes(needle))
  ).slice(0, 50);
}
