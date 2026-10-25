import { RUN_ID, projectReasoningState, summarizeEventPage } from "../../../../../lib/live-schema.ts";
import { potbotConfig, upstream, upstreamFailure } from "../../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

async function smallJSON(response: Response): Promise<unknown> {
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength > 2_000_000) throw new Error("Oversized reasoning response");
  return JSON.parse(new TextDecoder().decode(bytes));
}

export async function GET(request: Request, { params }: { params: Promise<{ runId: string }> }): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const config = potbotConfig();
  if (!config) return safeError("Potbot backend is not configured.", 503);
  const { runId } = await params;
  if (!RUN_ID.test(runId)) return safeError("Invalid reasoning run identifier.", 400);
  const url = new URL(request.url);
  const rawAfter = url.searchParams.get("after") ?? "0";
  if (!/^\d{1,15}$/.test(rawAfter) || Number(rawAfter) > Number.MAX_SAFE_INTEGER) return safeError("Invalid event cursor.", 400);
  const after = Number(rawAfter);
  try {
    const response = await upstream(config, `/v1/reasoning/${runId}`);
    if (!response.ok) return upstreamFailure(response);
    const state = await smallJSON(response);
    const graph = projectReasoningState(state);
    if (!graph) return safeError("Reasoning checkpoint has no graph projection yet.", 503);
    const pageResponse = await upstream(config, `/v1/reasoning/${runId}/events?after=${after}&limit=200`);
    if (!pageResponse.ok) return upstreamFailure(pageResponse);
    const page = summarizeEventPage(await smallJSON(pageResponse), after);
    if (!page) return safeError("Invalid reasoning event sequence.", 502);
    const run = state && typeof state === "object" && "state" in state && typeof state.state === "string" ? state.state : "unknown";
    return Response.json({ runId, graph, cursor: page.cursor, gap: page.gap,
      more: page.more, events: page.events, latestTypes: page.events.slice(-5).map(e => e.type), status: run,
    }, { headers: { "Cache-Control": "no-store" } });
  } catch { return safeError("Reasoning snapshot or events are not available.", 503); }
}
