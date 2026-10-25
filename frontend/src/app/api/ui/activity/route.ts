import { activityView, projectJobEvents, projectJobs } from "../../../../lib/activity.ts";
import { potbotConfig, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

async function limitedJSON(response: Response): Promise<unknown> {
  const contentLength = Number(response.headers.get("content-length") || 0);
  if (contentLength > 800_000) throw new Error("Backend activity page too large");
  const buffer = await response.arrayBuffer();
  if (buffer.byteLength > 800_000) throw new Error("Backend activity page too large");
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer));
}

/** Read-only projection of ACTUAL workspace job receipts, never progress simulations. */
export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const config = potbotConfig();
  if (!config) return safeError("Potbot backend not configured.", 503);
  const url = new URL(request.url);
  if ([...url.searchParams.keys()].some(k => k !== "after")) return safeError("Unrecognized activity query.", 400);
  const value = url.searchParams.get("after") ?? "0";
  if (!/^\d{1,15}$/.test(value) || !Number.isSafeInteger(Number(value))) return safeError("Invalid activity cursor.", 400);
  const after = Number(value);
  try {
    const [jobsResponse, eventsResponse] = await Promise.all([
      upstream(config, "/v1/jobs?limit=50"),
      upstream(config, `/v1/jobs/events?after=${after}&limit=100`),
    ]);
    if (!jobsResponse.ok) return upstreamFailure(jobsResponse);
    if (!eventsResponse.ok) return upstreamFailure(eventsResponse);
    const jobs = projectJobs(await limitedJSON(jobsResponse));
    const events = projectJobEvents(await limitedJSON(eventsResponse), after);
    if (!jobs || !events) return safeError("Invalid backend activity projection.", 502);
    return Response.json({ workspaceId: config.workspaceId, ...activityView(jobs, events) },
      { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Workspace activity is temporarily unavailable.", 503); }
}
