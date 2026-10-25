import { browserProxy } from "../../../../../../../lib/server/xpra-browser.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function POST(request: Request, context: { params: Promise<{ sessionId: string }> }) {
  const { sessionId } = await context.params;
  return browserProxy(request, sessionId);
}
