/**
 * The Auth.js `jwt` and `session` callbacks.
 *
 * Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/auth-callbacks.ts.
 *
 * v2 changes:
 *  * the token keeps what calling the v2 API as the user needs -- the access
 *    token, when it expires, and the refresh token (which never reaches the
 *    session, so never reaches a page);
 *  * the user's role in the tenant is asked of the API at sign-in and at every
 *    refresh, and is what the portal's gate reads. Authentik groups are still
 *    copied, for display, but they are hints (PLAN.md §4.2), not authorization;
 *  * an expired access token is refreshed only on an explicit update
 *    (`trigger === "update"`: a route handler or server action, which can
 *    write the session cookie). Rendering a page can't write it, and a refresh
 *    whose rotated refresh token is then thrown away spends the refresh token.
 */
import type { Account, Profile, Session, User } from "next-auth";
import type { JWT } from "next-auth/jwt";
import { getMyMembership, type Membership } from "./fishsense-api";
import { refreshTokens, type RefreshedTokens } from "./oidc";

/** Refresh this many seconds early, so a token is not spent as it expires. */
export const EXPIRY_SKEW_SECONDS = 30;

export type AuthDeps = {
  membership: (accessToken: string) => Promise<Membership | null>;
  refresh: (refreshToken: string) => Promise<RefreshedTokens>;
  /** Seconds since the epoch. */
  now: () => number;
};

const defaultDeps: AuthDeps = {
  membership: getMyMembership,
  refresh: refreshTokens,
  now: () => Math.floor(Date.now() / 1000),
};

interface AuthentikProfileLike extends Profile {
  groups?: string[];
}

interface JwtCallbackArgs {
  token: JWT;
  account?: Account | null;
  profile?: AuthentikProfileLike;
  user?: User;
  trigger?: "signIn" | "signUp" | "update";
}

/** Ask the API what the holder of `accessToken` may do; never throws. */
async function withMembership(token: JWT, accessToken: string, deps: AuthDeps) {
  try {
    const membership = await deps.membership(accessToken);
    token.role = membership?.role ?? null;
    token.isAdmin = membership?.isAdmin === true;
    delete token.membershipError;
  } catch (error) {
    // Fail closed: signing in still works, and the portal says why it is
    // shut, but no rights are assumed from an unanswered question.
    token.role = null;
    token.isAdmin = false;
    token.membershipError = error instanceof Error ? error.message : "unavailable";
  }
}

export async function jwtCallback(
  { token, account, profile, user, trigger }: JwtCallbackArgs,
  deps: AuthDeps = defaultDeps,
): Promise<JWT> {
  if (account) {
    if (typeof account.access_token === "string") {
      token.accessToken = account.access_token;
    }
    if (typeof account.refresh_token === "string") {
      token.refreshToken = account.refresh_token;
    }
    token.accessTokenExpiresAt =
      typeof account.expires_at === "number"
        ? account.expires_at
        : deps.now() + (typeof account.expires_in === "number" ? account.expires_in : 0);
    token.groups = Array.isArray(profile?.groups) ? profile.groups : [];
    if (user) {
      if (typeof user.id === "string") token.sub = user.id;
      if (typeof user.name === "string") token.name = user.name;
      if (typeof user.email === "string") token.email = user.email;
      if (typeof user.image === "string") token.picture = user.image;
    }
    delete token.error;
    if (typeof token.accessToken === "string") {
      await withMembership(token, token.accessToken, deps);
    } else {
      token.role = null;
      token.isAdmin = false;
    }
    return token;
  }

  const expiresAt = typeof token.accessTokenExpiresAt === "number" ? token.accessTokenExpiresAt : 0;
  if (trigger !== "update" || expiresAt - EXPIRY_SKEW_SECONDS > deps.now()) {
    return token;
  }

  if (typeof token.refreshToken !== "string") {
    token.error = "RefreshAccessTokenError";
    token.isAdmin = false;
    return token;
  }
  let refreshed: RefreshedTokens;
  try {
    refreshed = await deps.refresh(token.refreshToken);
  } catch {
    token.error = "RefreshAccessTokenError";
    token.isAdmin = false;
    return token;
  }
  token.accessToken = refreshed.accessToken;
  token.accessTokenExpiresAt = refreshed.expiresAt;
  token.refreshToken = refreshed.refreshToken;
  delete token.error;
  // A role granted or revoked since sign-in takes effect here.
  await withMembership(token, refreshed.accessToken, deps);
  return token;
}

interface SessionCallbackArgs {
  session: Session;
  token: JWT;
}

export async function sessionCallback({ session, token }: SessionCallbackArgs): Promise<Session> {
  if (typeof token.accessToken === "string") {
    session.accessToken = token.accessToken;
  }
  if (typeof token.accessTokenExpiresAt === "number") {
    session.accessTokenExpiresAt = token.accessTokenExpiresAt;
  }
  if (typeof token.sub === "string") session.user.id = token.sub;
  if (typeof token.name === "string") session.user.name = token.name;
  if (typeof token.email === "string") session.user.email = token.email;
  if (typeof token.picture === "string") session.user.image = token.picture;
  session.user.groups = Array.isArray(token.groups) ? token.groups : [];
  session.role = typeof token.role === "string" ? token.role : null;
  session.isAdmin = token.isAdmin === true;
  if (typeof token.error === "string") session.error = token.error;
  if (typeof token.membershipError === "string") {
    session.membershipError = token.membershipError;
  }
  return session;
}
