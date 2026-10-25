import { apiSetupRequest } from "../../../../../../lib/server/api-setup.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function PUT(request: Request, context: { params: Promise<{ provider: string }> }): Promise<Response> {
  return apiSetupRequest(request, "PUT", (await context.params).provider);
}
export async function DELETE(request: Request, context: { params: Promise<{ provider: string }> }): Promise<Response> {
  return apiSetupRequest(request, "DELETE", (await context.params).provider);
}
