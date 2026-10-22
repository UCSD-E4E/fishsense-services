/**
 * What the app shell shows of the session (docs/design/README.md, Tenants).
 *
 * The shell is in the root layout, which no error boundary covers: an auth
 * failure there (a missing AUTH_SECRET, an unreachable provider) would take
 * every page down, the public ones included. So a failure renders the shell
 * signed out, and is logged; Next's own control-flow errors still pass. Who is staff is the portal's own rule
 * (`isPortalAuthorized`), so Labeling never leads to a portal that refuses.
 */
import { unstable_rethrow } from "next/navigation";
import type { Session } from "next-auth";
import { isPortalAuthorized } from "./authz";

export type ShellState = {
  signedIn: boolean;
  name: string | null;
  staff: boolean;
  /** For asking the API the user's tenants, for the switcher. */
  accessToken: string | null;
};

const SIGNED_OUT: ShellState = { signedIn: false, name: null, staff: false, accessToken: null };

export async function shellState(load: () => Promise<Session | null>): Promise<ShellState> {
  let session: Session | null;
  try {
    session = await load();
  } catch (error) {
    // Next's control flow (dynamic rendering, redirect, not found) is not a
    // failure: it must reach Next, or a dynamic route renders as static.
    unstable_rethrow(error);
    console.error("app shell: reading the session failed; rendering signed out", error);
    return SIGNED_OUT;
  }
  if (!session?.user) return SIGNED_OUT;
  return {
    signedIn: true,
    name: session.user.name ?? session.user.email ?? null,
    staff: isPortalAuthorized(session),
    accessToken: session.accessToken ?? null,
  };
}

/** Sign in, then come back to `pathname` + `search`. */
export function signInHref(pathname: string, search: string): string {
  return `/api/auth/signin?callbackUrl=${encodeURIComponent(`${pathname}${search}`)}`;
}
