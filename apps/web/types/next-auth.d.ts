// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/types/next-auth.d.ts.
// v2 adds what calling the v2 API as the user needs, and the user's role in
// the tenant (lib/auth-callbacks.ts). The refresh token is on the JWT only.
import type { DefaultSession } from "next-auth";
import "next-auth/jwt";

declare module "next-auth" {
  interface Session {
    accessToken?: string;
    /** Seconds since the epoch. */
    accessTokenExpiresAt?: number;
    /** The user's role in the tenant, or null when they have none. */
    role?: string | null;
    /** Whether the API calls that role the tenant's admin. */
    isAdmin?: boolean;
    /** "RefreshAccessTokenError" when the token could not be refreshed. */
    error?: string;
    /** Why the role could not be read, when it could not. */
    membershipError?: string;
    user: {
      id?: string;
      groups: string[];
    } & DefaultSession["user"];
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    accessToken?: string;
    accessTokenExpiresAt?: number;
    refreshToken?: string;
    groups?: string[];
    role?: string | null;
    isAdmin?: boolean;
    error?: string;
    membershipError?: string;
  }
}
