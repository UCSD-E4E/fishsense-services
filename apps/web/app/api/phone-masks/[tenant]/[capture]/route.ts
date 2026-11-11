import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { isSlug, isUuid } from "@/lib/ids";
import { getPhoneMask } from "@/lib/phone-measurement";

/**
 * A phone capture's server mask, from this server (2026-11-10), as the
 * automatic mask is (`/api/masks`): the page tints the fish with it through
 * an SVG filter, which needs the image from the page's own origin. The API
 * decides who may see it, as the signed-in caller.
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

  const mask = await getPhoneMask(session.accessToken, tenant, capture);
  if (!mask) return new NextResponse("No mask for this capture", { status: 404 });
  return new NextResponse(mask, {
    status: 200,
    headers: { "content-type": "image/png", "cache-control": "private, max-age=3600" },
  });
}
