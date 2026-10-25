import { browserLeaseProxy } from "../../../../../../../lib/server/xpra-leases.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request, context: {params: Promise<{sessionId: string}>}) {
  return browserLeaseProxy(request, (await context.params).sessionId);
}
export async function POST(request: Request, context: {params: Promise<{sessionId: string}>}) {
  return browserLeaseProxy(request, (await context.params).sessionId);
}
