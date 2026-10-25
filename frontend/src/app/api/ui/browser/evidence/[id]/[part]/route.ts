import { galleryPart } from "../../../../../../../lib/server/browser-evidence.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request, context: {params: Promise<{id: string; part: string}>}) {
  const {id, part} = await context.params;
  return galleryPart(request, id, part);
}
