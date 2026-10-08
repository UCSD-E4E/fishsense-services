// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/auth-callbacks.test.ts.
//
// v2 changes, each pinned below:
//  * the session keeps what calling the v2 API needs: the access token, its
//    expiry and the refresh token (v1 kept the access token and never used it);
//  * the portal's authorization is the caller's role in the tenant, asked of
//    the API at sign-in and at every refresh -- not Authentik groups, which
//    PLAN.md §4.2 makes hints, not the source of truth (groups are still
//    carried, for display);
//  * an expired access token is refreshed only on an explicit update (a route
//    handler or server action, which can write the cookie), never while
//    rendering a page, which can't -- a refresh whose rotated token is thrown
//    away would spend the refresh token.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { type AuthDeps, jwtCallback, sessionCallback } from "./auth-callbacks";

const NOW = 1_800_000_000;
const baseToken = { sub: "u1", name: "User One", email: "u@e.com" };

let deps: AuthDeps & {
  membership: ReturnType<typeof vi.fn>;
  tenants: ReturnType<typeof vi.fn>;
  refresh: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  deps = {
    membership: vi.fn(async () => ({ role: "admin", isAdmin: true })),
    tenants: vi.fn(async () => []),
    refresh: vi.fn(async () => ({
      accessToken: "at-new",
      expiresAt: NOW + 300,
      refreshToken: "rt-new",
    })),
    now: () => NOW,
  };
});

const signIn = (extra: Record<string, unknown> = {}) => ({
  access_token: "at-123",
  refresh_token: "rt-123",
  expires_at: NOW + 300,
  provider: "authentik",
  ...extra,
});

describe("jwtCallback on sign-in", () => {
  it("copies access_token from account on initial sign-in", async () => {
    const result = await jwtCallback(
      {
        token: { ...baseToken },
        account: signIn() as never,
        profile: { groups: ["a", "b"] } as never,
      },
      deps,
    );
    expect(result.accessToken).toBe("at-123");
  });

  it("keeps the refresh token and when the access token expires", async () => {
    const result = await jwtCallback(
      { token: { ...baseToken }, account: signIn() as never, profile: {} as never },
      deps,
    );
    expect(result.refreshToken).toBe("rt-123");
    expect(result.accessTokenExpiresAt).toBe(NOW + 300);
  });

  it("derives the expiry from expires_in when expires_at is absent", async () => {
    const result = await jwtCallback(
      {
        token: { ...baseToken },
        account: signIn({ expires_at: undefined, expires_in: 120 }) as never,
        profile: {} as never,
      },
      deps,
    );
    expect(result.accessTokenExpiresAt).toBe(NOW + 120);
  });

  it("copies groups from profile on initial sign-in", async () => {
    const result = await jwtCallback(
      {
        token: { ...baseToken },
        account: signIn() as never,
        profile: { groups: ["fishsense-admins", "labelers"] } as never,
      },
      deps,
    );
    expect(result.groups).toEqual(["fishsense-admins", "labelers"]);
  });

  it("copies user identity (name/email/sub/picture) onto token on sign-in", async () => {
    const result = await jwtCallback(
      {
        token: {},
        account: signIn() as never,
        profile: {} as never,
        user: { id: "u-42", name: "Alice", email: "alice@e.com", image: "https://x/y.png" } as never,
      },
      deps,
    );
    expect(result.sub).toBe("u-42");
    expect(result.name).toBe("Alice");
    expect(result.email).toBe("alice@e.com");
    expect(result.picture).toBe("https://x/y.png");
  });

  it("defaults groups to [] when profile has none", async () => {
    const result = await jwtCallback(
      { token: { ...baseToken }, account: signIn() as never, profile: {} as never },
      deps,
    );
    expect(result.groups).toEqual([]);
  });

  it("asks the API for the user's role with the fresh access token", async () => {
    const result = await jwtCallback(
      { token: { ...baseToken }, account: signIn() as never, profile: {} as never },
      deps,
    );
    expect(deps.membership).toHaveBeenCalledExactlyOnceWith("at-123");
    expect(result.role).toBe("admin");
    expect(result.isAdmin).toBe(true);
  });

  it("records a non-member as having no role", async () => {
    deps.membership.mockResolvedValue(null);
    const result = await jwtCallback(
      { token: { ...baseToken }, account: signIn() as never, profile: {} as never },
      deps,
    );
    expect(result.role).toBeNull();
    expect(result.isAdmin).toBe(false);
  });

  it("records which tenants a non-member belongs to instead", async () => {
    // A partner who enrolled through their org's invite is a member of their
    // org's tenant (the API joins them on the `org` claim), not the lab's.
    deps.membership.mockResolvedValue(null);
    deps.tenants.mockResolvedValue([
      { slug: "conservation-angler", name: "Conservation Angler", role: "member", isAdmin: false },
    ]);
    const result = await jwtCallback(
      { token: { ...baseToken }, account: signIn() as never, profile: {} as never },
      deps,
    );
    expect(deps.tenants).toHaveBeenCalledExactlyOnceWith("at-123");
    expect(result.otherTenants).toEqual(["Conservation Angler"]);
    expect(result.isAdmin).toBe(false);
  });

  it("does not ask a member of this tenant for others", async () => {
    const result = await jwtCallback(
      {
        token: { ...baseToken, otherTenants: ["stale"] },
        account: signIn() as never,
        profile: {} as never,
      },
      deps,
    );
    expect(deps.tenants).not.toHaveBeenCalled();
    expect(result.otherTenants).toBeUndefined();
  });

  it("still signs a non-member in when their other tenants can't be read", async () => {
    // Only the explanation suffers; the answer that matters (no role) stands.
    deps.membership.mockResolvedValue(null);
    deps.tenants.mockRejectedValue(new Error("api down"));
    const result = await jwtCallback(
      { token: { ...baseToken }, account: signIn() as never, profile: {} as never },
      deps,
    );
    expect(result.role).toBeNull();
    expect(result.otherTenants).toEqual([]);
    expect(result.membershipError).toBeUndefined();
  });

  it("fails closed, and says so, when the API cannot answer", async () => {
    // Signing in must still work (the page explains the problem), but no
    // admin rights are assumed from an unanswered question.
    deps.membership.mockRejectedValue(new Error("api down"));
    const result = await jwtCallback(
      { token: { ...baseToken }, account: signIn() as never, profile: {} as never },
      deps,
    );
    expect(result.isAdmin).toBe(false);
    expect(result.role).toBeNull();
    expect(result.membershipError).toMatch(/api down/);
  });

  it("does not take authorization from Authentik groups", async () => {
    // v1 authorized on the group `FishSense-Prod-Admins`. v2's API owns
    // authorization (PLAN.md §4.2): a member in that group is still a member.
    deps.membership.mockResolvedValue({ role: "member", isAdmin: false });
    const result = await jwtCallback(
      {
        token: { ...baseToken },
        account: signIn() as never,
        profile: { groups: ["FishSense-Prod-Admins"] } as never,
      },
      deps,
    );
    expect(result.isAdmin).toBe(false);
  });
});

describe("jwtCallback afterwards", () => {
  const signedIn = {
    ...baseToken,
    accessToken: "at-prev",
    refreshToken: "rt-prev",
    accessTokenExpiresAt: NOW + 100,
    groups: ["x"],
    role: "admin",
    isAdmin: true,
  };

  it("returns token unchanged on subsequent calls (no account)", async () => {
    const result = await jwtCallback(
      { token: { ...signedIn }, account: null, profile: undefined },
      deps,
    );
    expect(result).toEqual(signedIn);
    expect(deps.refresh).not.toHaveBeenCalled();
    expect(deps.membership).not.toHaveBeenCalled();
  });

  it("never refreshes while rendering, even when expired", async () => {
    const expired = { ...signedIn, accessTokenExpiresAt: NOW - 1 };
    const result = await jwtCallback({ token: { ...expired }, account: null }, deps);
    expect(deps.refresh).not.toHaveBeenCalled();
    expect(result.accessToken).toBe("at-prev");
  });

  it("refreshes an expired token on an explicit update, and re-reads the role", async () => {
    deps.membership.mockResolvedValue({ role: "member", isAdmin: false });
    const expired = { ...signedIn, accessTokenExpiresAt: NOW - 1 };

    const result = await jwtCallback(
      { token: { ...expired }, account: null, trigger: "update" },
      deps,
    );

    expect(deps.refresh).toHaveBeenCalledExactlyOnceWith("rt-prev");
    expect(result.accessToken).toBe("at-new");
    expect(result.refreshToken).toBe("rt-new");
    expect(result.accessTokenExpiresAt).toBe(NOW + 300);
    // A role revoked since sign-in takes effect at the next refresh.
    expect(deps.membership).toHaveBeenCalledExactlyOnceWith("at-new");
    expect(result.isAdmin).toBe(false);
    expect(result.error).toBeUndefined();
  });

  it("refreshes a token about to expire", async () => {
    const nearly = { ...signedIn, accessTokenExpiresAt: NOW + 10 };
    await jwtCallback({ token: { ...nearly }, account: null, trigger: "update" }, deps);
    expect(deps.refresh).toHaveBeenCalled();
  });

  it("does not refresh a fresh token on update", async () => {
    await jwtCallback({ token: { ...signedIn }, account: null, trigger: "update" }, deps);
    expect(deps.refresh).not.toHaveBeenCalled();
  });

  it("marks the session and drops admin rights when the refresh fails", async () => {
    deps.refresh.mockRejectedValue(new Error("invalid_grant"));
    const expired = { ...signedIn, accessTokenExpiresAt: NOW - 1 };

    const result = await jwtCallback(
      { token: { ...expired }, account: null, trigger: "update" },
      deps,
    );

    expect(result.error).toBe("RefreshAccessTokenError");
    expect(result.isAdmin).toBe(false);
  });

  it("cannot refresh without a refresh token", async () => {
    const expired = { ...signedIn, refreshToken: undefined, accessTokenExpiresAt: NOW - 1 };
    const result = await jwtCallback(
      { token: { ...expired }, account: null, trigger: "update" },
      deps,
    );
    expect(deps.refresh).not.toHaveBeenCalled();
    expect(result.error).toBe("RefreshAccessTokenError");
  });
});

describe("sessionCallback", () => {
  it("surfaces accessToken, identity, groups and the role from token onto session", async () => {
    const result = await sessionCallback({
      session: { user: {}, expires: "2099-01-01" } as never,
      token: {
        sub: "u-1",
        name: "User One",
        email: "u@e.com",
        picture: "https://x/y.png",
        accessToken: "at-9",
        accessTokenExpiresAt: NOW + 5,
        refreshToken: "rt-9",
        groups: ["g1"],
        role: "admin",
        isAdmin: true,
      } as never,
    });
    expect(result.accessToken).toBe("at-9");
    expect(result.accessTokenExpiresAt).toBe(NOW + 5);
    expect(result.user.id).toBe("u-1");
    expect(result.user.name).toBe("User One");
    expect(result.user.email).toBe("u@e.com");
    expect(result.user.image).toBe("https://x/y.png");
    expect(result.user.groups).toEqual(["g1"]);
    expect(result.role).toBe("admin");
    expect(result.isAdmin).toBe(true);
  });

  it("never exposes the refresh token", async () => {
    const result = await sessionCallback({
      session: { user: {}, expires: "2099-01-01" } as never,
      token: { ...baseToken, refreshToken: "rt-secret" } as never,
    });
    expect(JSON.stringify(result)).not.toContain("rt-secret");
  });

  it("defaults groups to [] and admin to false when token has none", async () => {
    const result = await sessionCallback({
      session: { user: { name: "U", email: "u@e.com" }, expires: "2099-01-01" } as never,
      token: { ...baseToken } as never,
    });
    expect(result.user.groups).toEqual([]);
    expect(result.accessToken).toBeUndefined();
    expect(result.isAdmin).toBe(false);
    expect(result.role).toBeNull();
  });

  it("carries a refresh failure and a membership error through", async () => {
    const result = await sessionCallback({
      session: { user: {}, expires: "2099-01-01" } as never,
      token: {
        ...baseToken,
        error: "RefreshAccessTokenError",
        membershipError: "api down",
      } as never,
    });
    expect(result.error).toBe("RefreshAccessTokenError");
    expect(result.membershipError).toBe("api down");
  });

  it("carries a non-member's other tenants through", async () => {
    const result = await sessionCallback({
      session: { user: {}, expires: "2099-01-01" } as never,
      token: { ...baseToken, otherTenants: ["Conservation Angler"] } as never,
    });
    expect(result.otherTenants).toEqual(["Conservation Angler"]);
  });
});
