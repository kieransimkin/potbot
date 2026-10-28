import { agentXpraInventoryProxy } from "@/lib/server/agent-xpra";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export function GET(request: Request) { return agentXpraInventoryProxy(request); }
