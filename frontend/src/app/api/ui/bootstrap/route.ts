import { configuredWorkspace, workspaceConnections, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const configs = workspaceConnections();
  if (!configs.length) return safeError("Potbot workspace connections are unavailable.", 503);
  try {
    // Verify each configured bearer at the exact named workspace identity.
    // Never expose an unverified workspace or silently relabel credentials.
    const workspaces = [];
    for (const config of configs) {
      const response = await upstream(config, "/v1/ui/workspace");
      if (!response.ok) return upstreamFailure(response);
      const bytes = await response.arrayBuffer();
      if (bytes.byteLength > 4096) return safeError("Workspace identity response too large.", 502);
      const raw: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      const record = raw && typeof raw === "object" ? raw as { workspace?: unknown; available?: unknown } : null;
      const workspace = record?.workspace && typeof record.workspace === "object" ? record.workspace as { id?: unknown; name?: unknown } : null;
      if (workspace?.id !== config.workspaceId || record?.available !== true ||
          typeof workspace.name !== "string" || !workspace.name.trim())
        return safeError("Workspace identity mismatch; refusing to expose connections.", 502);
      workspaces.push(configuredWorkspace({ ...config, workspaceName: workspace.name.slice(0,80) }));
    }
    return Response.json({ source: "operator_scoped", workspaces,
      features: { chatStreaming: true, reasoningSnapshots: true,
        nativeWorkspaceDiscovery: false, configuredWorkspaceConnections: workspaces.length,
        durableChatSessions: true },
    }, { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Cannot verify Potbot workspace connections.", 503); }
}
