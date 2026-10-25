import { configuredWorkspace, potbotConfig, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const config = potbotConfig();
  if (!config) return safeError("Potbot backend is not configured for this UI.", 503);
  try {
    // This probes the bearer token against the real service. It does NOT claim
    // multi-workspace discovery or persistent chat sessions already exist.
    const response = await upstream(config, "/v1/models");
    if (!response.ok) return upstreamFailure(response);
    return Response.json({ source: "operator_scoped", workspaces: [configuredWorkspace(config)],
      features: { chatStreaming: true, reasoningSnapshots: true,
        nativeWorkspaceDiscovery: false, durableChatSessions: false },
    }, { headers: { "Cache-Control": "no-store" } });
  } catch { return safeError("Cannot connect to the Potbot backend.", 503); }
}
