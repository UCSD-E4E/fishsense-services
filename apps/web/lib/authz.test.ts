// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/authz.test.ts.
//
// v2 change: the portal is for the tenant's admins, as the v2 API records
// them (memberships.role = "admin"), not for an Authentik group named in
// PORTAL_ALLOWED_GROUPS. The rules that made v1's gate a gate are kept: it
// fails closed, and it matches exactly (the API does the matching).
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Session } from "next-auth";
import {
  accessTokenIsFresh,
  capturesGate,
  explainDenial,
  isPortalAuthorized,
  portalAccess,
} from "./authz";
import { refreshPath } from "./session-refresh";

const NOW = 1_800_000_000;

afterEach(() => {
  vi.unstubAllEnvs();
});

function session(fields: Partial<Session> & { groups?: string[] } = {}): Session {
  const { groups, ...rest } = fields;
  return {
    user: { groups: groups ?? [] },
    expires: "",
    accessToken: "at",
    accessTokenExpiresAt: NOW + 300,
    role: "admin",
    isAdmin: true,
    ...rest,
  } as unknown as Session;
}

describe("portalAccess", () => {
  it("allows the tenant's admin", () => {
    expect(portalAccess(session())).toEqual({ ok: true });
    expect(isPortalAuthorized(session())).toBe(true);
  });

  it("denies a member who is not an admin", () => {
    // The whole point: signing in proves an Authentik account, which is the
    // whole university realm, and being a member is not being an admin.
    expect(portalAccess(session({ role: "member", isAdmin: false }))).toEqual({
      ok: false,
      reason: "not-an-admin",
    });
  });

  it("denies a user with no membership in the tenant", () => {
    expect(portalAccess(session({ role: null, isAdmin: false }))).toEqual({
      ok: false,
      reason: "not-a-member",
    });
  });

  it("fails closed when the API could not say", () => {
    // Fail CLOSED. The portal writes calibration sources, which change
    // measured fish lengths; an unanswered question is not a yes.
    expect(
      portalAccess(session({ role: null, isAdmin: false, membershipError: "api down" })),
    ).toEqual({ ok: false, reason: "unavailable" });
  });

  it("denies a session whose refresh failed, even if it once was an admin", () => {
    expect(portalAccess(session({ error: "RefreshAccessTokenError" }))).toEqual({
      ok: false,
      reason: "expired",
    });
  });

  it("requires isAdmin itself, not a role that looks like one", () => {
    // The API decides who is an admin (an exact match on `admin`); the web
    // never re-derives it from the role string.
    expect(isPortalAuthorized(session({ role: "admin", isAdmin: false }))).toBe(false);
    expect(isPortalAuthorized(session({ isAdmin: undefined }))).toBe(false);
  });

  it("denies when there is no session or no user", () => {
    expect(isPortalAuthorized(null)).toBe(false);
    expect(portalAccess(null)).toEqual({ ok: false, reason: "signed-out" });
    expect(isPortalAuthorized({ expires: "" } as unknown as Session)).toBe(false);
  });

  it("ignores Authentik groups and PORTAL_ALLOWED_GROUPS", () => {
    // v1's gate. Groups are hints (PLAN.md §4.2); the membership is the truth.
    vi.stubEnv("PORTAL_ALLOWED_GROUPS", "FishSense-Prod-Admins");
    expect(
      isPortalAuthorized(
        session({ groups: ["FishSense-Prod-Admins"], role: "member", isAdmin: false }),
      ),
    ).toBe(false);
    vi.stubEnv("PORTAL_ALLOWED_GROUPS", "");
    expect(isPortalAuthorized(session({ groups: [] }))).toBe(true);
  });
});

describe("accessTokenIsFresh", () => {
  it("is fresh with more than the skew left", () => {
    expect(accessTokenIsFresh(session({ accessTokenExpiresAt: NOW + 31 }), NOW)).toBe(true);
  });

  it("is stale inside the skew, and after expiry", () => {
    // Stale a little early, so a token is not spent on a request that lands
    // after it has expired.
    expect(accessTokenIsFresh(session({ accessTokenExpiresAt: NOW + 29 }), NOW)).toBe(false);
    expect(accessTokenIsFresh(session({ accessTokenExpiresAt: NOW - 1 }), NOW)).toBe(false);
  });

  it("is stale with no token or no expiry", () => {
    expect(accessTokenIsFresh(session({ accessToken: undefined }), NOW)).toBe(false);
    expect(
      accessTokenIsFresh(session({ accessTokenExpiresAt: undefined }), NOW),
    ).toBe(false);
    expect(accessTokenIsFresh(null, NOW)).toBe(false);
  });
});

describe("explainDenial", () => {
  // The portal index is the dead end every denied page redirects to, so it
  // has to say why (v1 did, for its group check) and what to do about it.
  it("tells a member who is not an admin that the role is read at sign-in", () => {
    expect(explainDenial("not-an-admin", "lab")).toMatch(/admin role in the lab tenant/);
    expect(explainDenial("not-an-admin", "lab")).toMatch(/sign out and back in/);
  });

  it("tells a non-member they have no membership", () => {
    expect(explainDenial("not-a-member", "lab")).toMatch(/not a member of the lab tenant/);
  });

  it("tells a partner which tenant is theirs, and not to ask for the lab's", () => {
    const text = explainDenial("not-a-member", "lab", ["Conservation Angler"]);
    expect(text).toMatch(/belongs to Conservation Angler/);
    expect(text).toMatch(/lab tenant/);
    expect(text).not.toMatch(/ask one to add you/);
  });

  it("does not blame the user for an outage", () => {
    expect(explainDenial("unavailable", "lab")).toMatch(/could not confirm/);
  });

  it("asks an expired session to sign in again", () => {
    expect(explainDenial("expired", "lab")).toMatch(/sign in again/i);
  });
});

describe("capturesGate", () => {
  const PATH = "/captures/lab/abc";
  const SIGN_IN = `/api/auth/signin?callbackUrl=${encodeURIComponent(PATH)}`;

  it("lets a signed-in member with a fresh token through", () => {
    // Any member, not only admins: the API decides what each one may see.
    expect(capturesGate(session({ role: "member", isAdmin: false }), PATH, NOW)).toEqual({
      ok: true,
    });
  });

  it("sends a signed-out visitor to sign in", () => {
    expect(capturesGate(null, PATH, NOW)).toEqual({ ok: false, redirect: SIGN_IN });
  });

  it("sends a session whose refresh failed to sign in, not through with its stale token", () => {
    // Review of v0.6.1: the gate let a `session.error` session through, so the
    // page called the API with a dead token and showed its 401 as an empty
    // list. The portal treats the same session as expired.
    expect(
      capturesGate(session({ error: "RefreshAccessTokenError", accessTokenExpiresAt: NOW - 1 }), PATH, NOW),
    ).toEqual({ ok: false, redirect: SIGN_IN });
  });

  it("renews a stale token first", () => {
    expect(capturesGate(session({ accessTokenExpiresAt: NOW - 1 }), PATH, NOW)).toEqual({
      ok: false,
      redirect: refreshPath(PATH),
    });
  });
});
