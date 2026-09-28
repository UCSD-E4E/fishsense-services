import { unstable_update } from "@/auth";
import { accessTokenIsFresh } from "@/lib/authz";
import { safeCallbackPath } from "@/lib/session-refresh";

/**
 * Renews the signed-in user's access token, then sends them back.
 *
 * New in v2. A portal page can't write the session cookie, so a page whose
 * token has expired redirects here (app/portal/guard.ts). `unstable_update`
 * runs the jwt callback with `trigger: "update"`, which refreshes the token
 * and re-reads the user's role (lib/auth-callbacks.ts), and writes the cookie.
 *
 * If the refresh fails, or leaves the token no fresher, the user signs in
 * again rather than bouncing between this route and the page.
 *
 * The redirect is relative: behind the proxy this request's own URL is the
 * container's listen address (the reason v1 had to set AUTH_URL).
 */
export async function GET(request: Request) {
  const callback = safeCallbackPath(new URL(request.url).searchParams.get("callbackUrl"));
  const session = await unstable_update({});
  const target =
    session?.user && !session.error && accessTokenIsFresh(session)
      ? callback
      : `/api/auth/signin?callbackUrl=${encodeURIComponent(callback)}`;
  return new Response(null, { status: 307, headers: { location: target } });
}
