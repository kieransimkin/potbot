import { appProxy } from "../../../../../../../lib/server/xpra-apps.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export function POST(request: Request, context: {params: Promise<{sessionId: string; operation: string}>}) {
  return context.params.then(({sessionId, operation}) =>
    ["attach", "detach", "terminate"].includes(operation) ?
      appProxy(request, operation as "attach" | "detach" | "terminate", sessionId) :
      new Response(null, {status:404}));
}
