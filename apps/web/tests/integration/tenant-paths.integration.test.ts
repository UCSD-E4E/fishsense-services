// The tenant moved into the path (docs/design/README.md, Tenants): the built
// image's middleware sends old links to their new home, signed in or not.
import { describe, expect, it } from "vitest";

const WEB_URL = process.env.FISHSENSE_WEB_URL ?? "http://localhost:3000";

async function location(path: string): Promise<{ status: number; to: string | null }> {
  const res = await fetch(`${WEB_URL}${path}`, { cache: "no-store", redirect: "manual" });
  const to = res.headers.get("location");
  return { status: res.status, to: to && new URL(to, WEB_URL).pathname + new URL(to, WEB_URL).search };
}

describe("old links redirect to the tenant's path (against the running container)", () => {
  it("moves a slug from the middle of the path", async () => {
    expect(await location("/fish/lab/c1/f1")).toEqual({ status: 308, to: "/lab/fish/c1/f1" });
    expect(await location("/missions/lab/383")).toEqual({
      status: 308,
      to: "/lab/data/missions/383",
    });
  });

  it("moves ?tenant= and keeps the rest of the query", async () => {
    expect(await location("/fish?tenant=lab&device=mobile")).toEqual({
      status: 308,
      to: "/lab/fish?device=mobile",
    });
  });

  it("does not take a file a browser asks for as a tenant", async () => {
    // v0.22.0 sent /favicon.ico to /favicon.ico/fish, and on to sign-in.
    expect((await location("/favicon.ico")).status).toBe(404);
    expect((await location("/portal.ico/fish")).status).toBe(404);
  });

  it("sends a signed-out visitor to a tenant page to sign in, coming back there", async () => {
    const { status, to } = await location("/lab/fish");

    expect(status).toBe(307);
    expect(to).toContain("/api/auth/signin");
    expect(to).toContain(encodeURIComponent("/lab/fish"));
  });
});
