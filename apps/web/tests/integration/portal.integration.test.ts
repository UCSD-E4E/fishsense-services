// Ported from fishsense-lite@77e8f8e5
// apps/fishsense-lite-web/tests/integration/portal.integration.test.ts.
//
// v2 change: the gate is the user's admin role in the tenant, as the v2 API
// reported it at sign-in (carried on the session), not an Authentik group;
// and a session whose access token has expired is renewed first.
import { encode } from "next-auth/jwt";
import { describe, expect, it } from "vitest";

const WEB_URL = process.env.FISHSENSE_WEB_URL ?? "http://localhost:3000";

// Must match the AUTH_SECRET the container under test booted with (compose.yml's
// `web` service, and the CI job) — the page's auth() call decrypts the session
// cookie with it, and an undecryptable cookie reads as signed-out (a 307).
const AUTH_SECRET = process.env.AUTH_SECRET ?? "fishsense-web-dev-only-auth-secret";

// Local stack runs over http, so the cookie name has no `__Secure-`
// prefix. JWE encryption salt defaults to the cookie name in Auth.js v5.
const SESSION_COOKIE_NAME = "authjs.session-token";

const IN_AN_HOUR = () => Math.floor(Date.now() / 1000) + 3600;

/** Mint a session JWE the way next-auth does after a real OIDC callback. */
async function sessionCookie(claims: Record<string, unknown>) {
  return encode({
    token: {
      sub: "test-user-id",
      name: "Integration Test User",
      email: "integration-test@fishsense.local",
      groups: ["some-authentik-group"],
      accessToken: "not-used-by-the-index",
      accessTokenExpiresAt: IN_AN_HOUR(),
      ...claims,
    },
    secret: AUTH_SECRET,
    salt: SESSION_COOKIE_NAME,
    maxAge: 60 * 60,
  });
}

async function getPortal(cookie?: string) {
  return fetch(`${WEB_URL}/portal`, {
    cache: "no-store",
    redirect: "manual",
    ...(cookie ? { headers: { cookie: `${SESSION_COOKIE_NAME}=${cookie}` } } : {}),
  });
}

describe("/portal SSR auth gate (against the running container)", () => {
  it("redirects signed-out GET /portal to the next-auth sign-in route", async () => {
    const res = await fetch(`${WEB_URL}/portal`, { cache: "no-store", redirect: "manual" });
    expect(res.status).toBe(307);
    const location = res.headers.get("location") ?? "";
    expect(location).toContain("/api/auth/signin");
    expect(location).toContain("callbackUrl=%2Fportal");
  });

  it("renders the portal for the tenant's admin", async () => {
    const res = await getPortal(await sessionCookie({ role: "admin", isAdmin: true }));

    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("Integration Test User");
    expect(body).toContain("integration-test@fishsense.local");
    expect(body).toContain("Dive calibration links");
  });

  it("renders a dead end, not the portal, for a member who is not an admin", async () => {
    // Asserted as an absence as well as a presence, because the failure that
    // matters is the gate silently disappearing — a test that only checked
    // for the denial text would still pass if the page rendered BOTH.
    const res = await getPortal(await sessionCookie({ role: "member", isAdmin: false }));

    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("needs the admin role");
    expect(body).not.toContain("Dive calibration links");
  });

  it("does not take the portal from an Authentik group", async () => {
    // v1's gate was the group FishSense-Prod-Admins; v2's is the API's role.
    const res = await getPortal(
      await sessionCookie({ groups: ["FishSense-Prod-Admins"], role: null, isAdmin: false }),
    );

    const body = await res.text();
    expect(body).toContain("not a member");
    expect(body).not.toContain("Dive calibration links");
  });

  it("does not redirect a signed-in but unauthorized user into a sign-in loop", async () => {
    // A denied user is already signed in, so redirecting them to sign-in
    // would loop forever. The dead end has to be a 200 with a sign-out
    // affordance instead.
    const res = await getPortal(await sessionCookie({ role: null, isAdmin: false }));

    expect(res.status).toBe(200);
    expect(res.headers.get("location")).toBeNull();
  });

  it("renews an expired access token before deciding", async () => {
    const res = await getPortal(
      await sessionCookie({ role: "admin", isAdmin: true, accessTokenExpiresAt: 1 }),
    );

    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toContain(
      "/api/session/refresh?callbackUrl=%2Fportal",
    );
  });
});
