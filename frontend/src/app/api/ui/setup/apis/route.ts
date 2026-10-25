import { apiSetupRequest } from "../../../../../lib/server/api-setup.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request): Promise<Response> {
  return apiSetupRequest(request, "GET");
}
