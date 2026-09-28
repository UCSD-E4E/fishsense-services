// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/auth.ts.
//
// v2 changes: the scopes add `org` (PLAN.md §4.2: v2's clients must request
// it, for the partner-tenant claim) and `offline_access` (a refresh token, so
// the user's access token can be renewed for calls to the v2 API), and
// `unstable_update` is exported for /api/session/refresh and the server
// actions, which can write the refreshed session cookie.
import NextAuth from "next-auth";
import Authentik from "next-auth/providers/authentik";
import { jwtCallback, sessionCallback } from "@/lib/auth-callbacks";
import { env } from "@/lib/env";

// Function-form config: env is read per-request, not at module load.
// `next build` imports this module to collect page data without AUTH_*
// env vars set, so reading env eagerly here would fail the build.
export const { auth, handlers, signIn, signOut, unstable_update } = NextAuth(() => ({
  secret: env.authSecret,
  trustHost: true,
  session: { strategy: "jwt" },
  providers: [
    Authentik({
      clientId: env.authAuthentikId,
      clientSecret: env.authAuthentikSecret,
      issuer: env.authAuthentikIssuer,
      // `groups` must be requested explicitly — the provider's default scope
      // is `openid profile email`. Groups are shown on the portal, no longer
      // used to authorize (lib/authz.ts). `org` carries a partner's tenant
      // (PLAN.md §4.2). `offline_access` asks Authentik for a refresh token.
      authorization: {
        params: { scope: "openid email profile groups org offline_access" },
      },
    }),
  ],
  callbacks: {
    jwt: (params) => jwtCallback(params),
    session: sessionCallback,
  },
}));
