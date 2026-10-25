import { appProxy } from "../../../../../lib/server/xpra-apps.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export function GET(request: Request) { return appProxy(request, "list"); }
export function POST(request: Request) { return appProxy(request, "start"); }
