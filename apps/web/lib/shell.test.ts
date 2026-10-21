import { describe, expect, it, vi } from "vitest";
import { redirect } from "next/navigation";
import type { Session } from "next-auth";
import { shellState, signInHref } from "./shell";

const admin = {
  user: { name: "Dev Admin" },
  role: "admin",
  isAdmin: true,
  accessToken: "t",
  expires: "2099-01-01T00:00:00Z",
} as unknown as Session;

describe("the shell's view of the session", () => {
  it("is signed out with nobody signed in", async () => {
    expect(await shellState(async () => null)).toEqual({
      signedIn: false,
      name: null,
      staff: false,
    });
  });

  it("names the user, and calls an admin staff", async () => {
    expect(await shellState(async () => admin)).toEqual({
      signedIn: true,
      name: "Dev Admin",
      staff: true,
    });
  });

  it("asks the portal's own rule who is staff", async () => {
    // A stale admin flag beside a failed membership read is not staff: the
    // portal would refuse them, so Labelling must not lead there.
    const stale = { ...admin, membershipError: "unavailable" } as unknown as Session;

    expect((await shellState(async () => stale)).staff).toBe(false);
  });

  it("renders signed out when auth itself fails, rather than taking the page down", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const state = await shellState(async () => {
      throw new Error("AUTH_SECRET missing");
    });

    expect(state).toEqual({ signedIn: false, name: null, staff: false });
    expect(error).toHaveBeenCalled();
  });
});

describe("Next's own signals pass through", () => {
  it("rethrows a control-flow error instead of rendering signed out", async () => {
    // auth() reads headers(), which throws to tell Next a route is dynamic;
    // swallowing that froze /_not-found as a static, signed-out page.
    await expect(
      shellState(async () => {
        redirect("/elsewhere");
      }),
    ).rejects.toThrow("NEXT_REDIRECT");
  });
});

describe("signing in from the shell", () => {
  it("comes back to the page it started from", () => {
    expect(signInHref("/fish", "?tenant=lab")).toBe(
      "/api/auth/signin?callbackUrl=%2Ffish%3Ftenant%3Dlab",
    );
    expect(signInHref("/", "")).toBe("/api/auth/signin?callbackUrl=%2F");
  });
});
