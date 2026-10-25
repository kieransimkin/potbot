import { apiSetupRequest } from "../../../../../../../lib/server/api-setup.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function POST(request: Request, context: { params: Promise<{ provider: string }> }): Promise<Response> {
  return apiSetupRequest(request, "POST", (await context.params).provider);
}
