import { describe, expect, it } from "vitest";
import { banner, otherTenants } from "./system-admin";

describe("the banner on a tenant a system admin is not in", () => {
  it("says nothing to a member", () => {
    expect(banner({ access: "member", unlockedUntil: null })).toBeNull();
  });

  it("says read-only until they unlock", () => {
    expect(banner({ access: "system_admin_read_only", unlockedUntil: null })).toEqual({
      kind: "read_only",
    });
  });

  it("says until when they may change it, once unlocked", () => {
    expect(
      banner({ access: "system_admin_unlocked", unlockedUntil: "2026-10-20T22:00:00Z" }),
    ).toEqual({ kind: "unlocked", until: "2026-10-20T22:00:00Z" });
  });

  it("fails closed: an access it does not know is read-only", () => {
    expect(banner({ access: "something_new", unlockedUntil: null })).toEqual({ kind: "read_only" });
  });
});

describe("the switcher's other tenants", () => {
  it("are every tenant the system admin is not a member of, by name", () => {
    const mine = [{ slug: "lab", name: "Lab" }];
    const all = [
      { slug: "partner", name: "Conservation Angler" },
      { slug: "lab", name: "Lab" },
      { slug: "acme", name: "Acme" },
    ];

    expect(otherTenants(mine, all).map((t) => t.slug)).toEqual(["acme", "partner"]);
  });
});
