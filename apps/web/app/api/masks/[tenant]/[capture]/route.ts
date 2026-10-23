import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { getAutomaticMask } from "@/lib/automatic";
import { isSlug, isUuid } from "@/lib/ids";

/**
 * A frame's automatic mask, from this server (2026-10-22). The review tool
 * snaps a fix's head and tail to the mask's pixels, as the app does, and a
 * page can read an image's pixels only when it comes from the page's own
 * origin. The API decides who may see it, as the signed-in caller: a frame
 * outside their tenant is a 404, the same as one with no mask.
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ tenant: string; capture: string }> },
) {
  const session = await auth();
  if (!session?.user || session.error || !session.accessToken) {
    return new NextResponse("Sign in", { status: 401 });
  }
  const { tenant, capture } = await params;
  if (!isSlug(tenant) || !isUuid(capture)) return new NextResponse("Not found", { status: 404 });

  const mask = await getAutomaticMask(session.accessToken, tenant, capture);
  if (!mask) return new NextResponse("No mask for this frame", { status: 404 });
  return new NextResponse(mask, {
    status: 200,
    headers: { "content-type": "image/png", "cache-control": "private, max-age=3600" },
  });
}
