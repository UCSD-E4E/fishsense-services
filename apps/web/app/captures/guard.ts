import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { accessTokenIsFresh } from "@/lib/authz";
import { refreshPath } from "@/lib/session-refresh";

/**
 * The gate for the captures pages: signed in, with a usable token -- any
 * member of a tenant, not only its admins (the portal's gate). What a user
 * may see is the API's decision, made on every call with their own token.
 */
export async function requireSignedIn(path: string) {
  const session = await auth();
  if (!session?.user) {
    redirect(`/api/auth/signin?callbackUrl=${encodeURIComponent(path)}`);
  }
  if (!session.error && !accessTokenIsFresh(session)) {
    redirect(refreshPath(path));
  }
  return session;
}
