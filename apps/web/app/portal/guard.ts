import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { accessTokenIsFresh, isPortalAuthorized } from "@/lib/authz";
import { refreshPath } from "@/lib/session-refresh";

/**
 * The gate every portal page under the index shares.
 *
 * Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/portal/guard.ts.
 *
 * Authenticated is not authorized — signing in only proves an account in the
 * Authentik realm. An unauthorized user is sent to `/portal`, which is the one
 * page that explains *why* they cannot get in and offers a sign-out; sending
 * them to the sign-in flow instead would loop forever for someone who is
 * already signed in and simply lacks the role.
 *
 * v2 change: an expired access token goes through `/api/session/refresh`
 * first, which renews it and re-reads the user's role from the API, so what
 * this gate decides on is at most one token lifetime old.
 *
 * This is a rendering decision, not the security boundary. Server actions are
 * public endpoints and re-check the session themselves — see
 * `calibration/actions.ts` — and the v2 API checks the role on its writes.
 */
export async function requirePortalUser(path: string) {
  const session = await auth();
  if (!session?.user) {
    redirect(`/api/auth/signin?callbackUrl=${encodeURIComponent(path)}`);
  }
  if (!session.error && !accessTokenIsFresh(session)) {
    redirect(refreshPath(path));
  }
  if (!isPortalAuthorized(session)) {
    redirect("/portal");
  }
  return session;
}
