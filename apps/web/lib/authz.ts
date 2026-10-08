import type { Session } from "next-auth";
import { EXPIRY_SKEW_SECONDS } from "./auth-callbacks";

/**
 * Authorization for `/portal`.
 *
 * Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/authz.ts.
 *
 * Signing in proves only that someone holds an account in the Authentik
 * realm — which is the whole university SSO population, not the FishSense
 * team. The portal rewrites a dive's calibration source, a live pipeline
 * input: a dive measured off a borrowed calibration runs -8..+2% error against
 * ~1% for its own, so the link changes reported fish lengths. And triage
 * writes annotations the sync reads as labels.
 *
 * v2 change: v1 admitted members of an Authentik group named in
 * `PORTAL_ALLOWED_GROUPS`. v2's API owns authorization (PLAN.md §4.2): the
 * portal is for the tenant's **admins** (memberships.role = "admin"), as the
 * API reports it -- at sign-in, and again at every token refresh. The API also
 * enforces the role itself on every write it serves.
 *
 * What made v1's check a check is kept. It **fails closed**: no answer from
 * the API, no membership, or a failed refresh all deny. And it matches
 * exactly: the API decides `is_admin`, and the web never re-derives it from a
 * role string.
 */

export type PortalAccess =
  | { ok: true }
  | {
      ok: false;
      reason: "signed-out" | "expired" | "unavailable" | "not-a-member" | "not-an-admin";
    };

export function portalAccess(session: Session | null): PortalAccess {
  if (!session?.user) return { ok: false, reason: "signed-out" };
  if (session.error) return { ok: false, reason: "expired" };
  if (session.membershipError) return { ok: false, reason: "unavailable" };
  if (session.role == null) return { ok: false, reason: "not-a-member" };
  if (session.isAdmin !== true) return { ok: false, reason: "not-an-admin" };
  return { ok: true };
}

/** True iff `session` belongs to a signed-in admin of the tenant. */
export function isPortalAuthorized(session: Session | null): boolean {
  return portalAccess(session).ok;
}

/**
 * Whether the session's access token can still be sent to the API.
 *
 * Stale a little before it expires (the same skew the refresh uses), so a
 * token is not spent on a request that arrives after it has lapsed.
 */
export function accessTokenIsFresh(
  session: Session | null,
  nowSeconds: number = Math.floor(Date.now() / 1000),
): boolean {
  if (!session?.accessToken) return false;
  const expiresAt = session.accessTokenExpiresAt;
  if (typeof expiresAt !== "number") return false;
  return expiresAt - EXPIRY_SKEW_SECONDS > nowSeconds;
}

type Denial = Extract<PortalAccess, { ok: false }>["reason"];

/**
 * What the portal's dead end says to a user it turned away, and why.
 * `otherTenants` names the tenants a non-member is in instead -- a partner's
 * org, joined automatically through its invite -- so they aren't sent to an
 * operator for a membership they shouldn't have.
 */
export function explainDenial(
  reason: Denial,
  tenant: string,
  otherTenants: readonly string[] = [],
): string {
  switch (reason) {
    case "not-an-admin":
      return `This account is a member, but the portal needs the admin role in the ${tenant} tenant. If you were recently made an admin, sign out and back in — your role is read when you sign in. Otherwise, ask an operator.`;
    case "not-a-member":
      if (otherTenants.length > 0) {
        return `This account belongs to ${otherTenants.join(", ")}. This portal manages the ${tenant} tenant's data, which your account is not part of.`;
      }
      return `This account is not a member of the ${tenant} tenant. Memberships are granted by an operator; ask one to add you, then sign out and back in.`;
    case "unavailable":
      return "The FishSense API could not confirm your role, so access is denied until it can. Sign out and back in in a moment; if it persists, the API may be down.";
    case "expired":
      return "Your sign-in has expired. Sign out and sign in again.";
    case "signed-out":
      return "You are not signed in.";
  }
}
