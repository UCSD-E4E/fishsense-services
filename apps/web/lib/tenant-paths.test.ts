import { describe, expect, it } from "vitest";
import {
  RESERVED_SLUGS,
  isTenantSlug,
  legacyRedirect,
  routeRequest,
  paths,
  switchHref,
  tenantOf,
} from "./tenant-paths";

describe("every tenant page lives under its slug (docs/design/README.md, Tenants)", () => {
  it("builds each page's path", () => {
    expect(paths.fish("lab")).toBe("/lab/fish");
    expect(paths.fishDetail("lab", "c1", "f1")).toBe("/lab/fish/c1/f1");
    expect(paths.data("lab")).toBe("/lab/data");
    expect(paths.deployment("lab", 4)).toBe("/lab/data/deployments/4");
    expect(paths.missions("lab")).toBe("/lab/data/missions");
    expect(paths.mission("lab", 383)).toBe("/lab/data/missions/383");
    expect(paths.phoneMissions("lab")).toBe("/lab/data/missions?source=mobile");
    expect(paths.capture("lab", "u1")).toBe("/lab/data/captures/u1");
  });

  it("escapes what it is given", () => {
    expect(paths.fishDetail("a b", "c/d", "e")).toBe("/a%20b/fish/c%2Fd/e");
  });

  it("reads the tenant from a path, and only from a tenant page", () => {
    expect(tenantOf("/lab/fish/c/f")).toBe("lab");
    expect(tenantOf("/lab")).toBe("lab");
    expect(tenantOf("/")).toBeNull();
    expect(tenantOf("/portal/triage")).toBeNull();
    expect(tenantOf("/api/auth/signin")).toBeNull();
    // Browsers ask for files the app may not serve; their 404 is no tenant's.
    expect(tenantOf("/favicon.ico")).toBeNull();
    expect(tenantOf("/apple-touch-icon.png")).toBeNull();
    expect(tenantOf("/Lab/fish")).toBeNull();
  });
});

describe("switching tenants keeps the section, not the record", () => {
  it("lands on the other tenant's same section", () => {
    expect(switchHref("/lab/fish/c1/f1", "angler")).toBe("/angler/fish");
    expect(switchHref("/lab/data/missions/383", "angler")).toBe("/angler/data/missions");
    expect(switchHref("/lab/data/deployments/4", "angler")).toBe("/angler/data");
    expect(switchHref("/lab/data", "angler")).toBe("/angler/data");
    expect(switchHref("/lab/data/captures/u1", "angler")).toBe("/angler/data/missions");
  });

  it("goes to the tenant's fish from anywhere else", () => {
    expect(switchHref("/", "angler")).toBe("/angler/fish");
    expect(switchHref("/portal", "angler")).toBe("/angler/fish");
  });
});

describe("old links redirect to the tenant's path", () => {
  const go = (path: string, last: string | null = null) => {
    const url = new URL(path, "https://fishsense.example");
    return legacyRedirect(url.pathname, url.searchParams, last);
  };

  it("moves a slug from the path", () => {
    expect(go("/fish/lab/c1/f1")).toBe("/lab/fish/c1/f1");
    expect(go("/missions/lab/383")).toBe("/lab/data/missions/383");
    expect(go("/deployments/lab/4")).toBe("/lab/data/deployments/4");
    expect(go("/captures/lab/u1")).toBe("/lab/data/captures/u1");
  });

  it("moves ?tenant= and keeps the other parameters", () => {
    expect(go("/fish?tenant=lab&device=mobile")).toBe("/lab/fish?device=mobile");
    expect(go("/deployments?tenant=lab")).toBe("/lab/data");
    expect(go("/missions?tenant=lab")).toBe("/lab/data/missions");
    // The capture list is retired (phase 7b): phone fish are in their missions.
    expect(go("/captures?tenant=lab&before=x")).toBe("/lab/data/missions?source=mobile");
  });

  it("uses the last tenant when the link names none", () => {
    expect(go("/fish", "angler")).toBe("/angler/fish");
    expect(go("/missions?purpose=survey", "angler")).toBe("/angler/data/missions?purpose=survey");
  });

  it("leaves it to the page when nothing says which tenant", () => {
    // The page signs the user in and takes their first tenant.
    expect(go("/fish")).toBeNull();
  });

  it("leaves everything else alone", () => {
    expect(go("/")).toBeNull();
    expect(go("/lab/fish")).toBeNull();
    expect(go("/portal/triage")).toBeNull();
    expect(go("/api/auth/signin")).toBeNull();
  });

  it("does not trust a reserved slug as a tenant", () => {
    expect(go("/fish?tenant=api")).toBeNull();
    expect(go("/fish", "portal")).toBeNull();
    expect(go("/fish?tenant=..%2Fapi")).toBeNull();
    expect(go("/fish/Not%20A%20Slug/c/f")).toBeNull();
  });
});

describe("a tenant cannot take a route's name", () => {
  it("reserves every top-level route the web serves", () => {
    for (const route of ["api", "fish", "missions", "deployments", "captures", "portal"]) {
      expect(RESERVED_SLUGS).toContain(route);
    }
  });
});

describe("what the middleware does with a request", () => {
  const route = (path: string, cookie: string | null = null) => {
    const url = new URL(path, "https://fishsense.example");
    return routeRequest(url.pathname, url.searchParams, cookie);
  };

  it("redirects an old link that names its tenant", () => {
    expect(route("/fish?tenant=lab")).toEqual({ redirect: "/lab/fish" });
    expect(route("/missions/lab/383")).toEqual({ redirect: "/lab/data/missions/383" });
  });

  it("leaves an old link that names no tenant to its page, even with a cookie", () => {
    // Only the page knows the user's memberships, so only it can trust the
    // cookie: a tenant they have left would be a 404.
    expect(route("/fish", "lab")).toEqual({});
  });

  it("remembers the tenant of a tenant page", () => {
    expect(route("/lab/fish")).toEqual({ remember: "lab" });
    expect(route("/lab/data/missions/383", "angler")).toEqual({ remember: "lab" });
  });

  it("does not rewrite the cookie it already has, nor remember a non-page", () => {
    expect(route("/lab/fish", "lab")).toEqual({});
    expect(route("/lab/whatever")).toEqual({});
    expect(route("/portal")).toEqual({});
    expect(route("/")).toEqual({});
    expect(route("/favicon.ico", "lab")).toEqual({});
  });
});

describe("a tenant route takes only a tenant's slug", () => {
  it("refuses a file a browser asks for, a reserved word, or a malformed slug", () => {
    expect(isTenantSlug("lab")).toBe(true);
    expect(isTenantSlug("conservation-angler")).toBe(true);
    // Each would render the tenant layout, and redirect into a tenant page.
    expect(isTenantSlug("favicon.ico")).toBe(false);
    expect(isTenantSlug("portal")).toBe(false);
    expect(isTenantSlug("Lab")).toBe(false);
    expect(isTenantSlug("_next")).toBe(false);
  });
});
