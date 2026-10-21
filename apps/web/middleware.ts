import { NextResponse, type NextRequest } from "next/server";
import { LAST_TENANT_COOKIE, routeRequest } from "@/lib/tenant-paths";

/** Old links to their tenant's path, and the last tenant opened remembered
 *  (`routeRequest`; docs/design/README.md, Tenants). */
export function middleware(request: NextRequest) {
  const { pathname, searchParams } = request.nextUrl;
  const cookie = request.cookies.get(LAST_TENANT_COOKIE)?.value ?? null;
  const routing = routeRequest(pathname, searchParams, cookie);
  if (routing.redirect) {
    return NextResponse.redirect(new URL(routing.redirect, request.url), 308);
  }
  const response = NextResponse.next();
  if (routing.remember) {
    response.cookies.set(LAST_TENANT_COOKIE, routing.remember, {
      path: "/",
      sameSite: "lax",
      httpOnly: true,
      secure: request.nextUrl.protocol === "https:",
      maxAge: 60 * 60 * 24 * 365,
    });
  }
  return response;
}

export const config = {
  // Not the API, Next's own files, or the public files.
  matcher: ["/((?!api/|_next/|favicon\\.png|logo\\.svg).*)"],
};
