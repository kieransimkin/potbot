import { agentXpraTicketProxy } from "@/lib/server/agent-xpra";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function POST(request: Request,
  context: {params: Promise<{agentId: string; sessionId: string}>}) {
  const {agentId, sessionId} = await context.params;
  return agentXpraTicketProxy(request, agentId, sessionId);
}
