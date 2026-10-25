import { galleryList } from "../../../../../lib/server/browser-evidence.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export function GET(request: Request) { return galleryList(request); }
