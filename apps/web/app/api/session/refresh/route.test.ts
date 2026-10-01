// New in v2: the route a portal page bounces through to renew its token.
import { afterEach, describe, expect, it, vi } from "vitest";

const { unstable_update } = vi.hoisted(() => ({ unstable_update: vi.fn() }));
vi.mock("@/auth", () => ({ unstable_update }));

import { GET } from "./route";

const FRESH = {
  user: { name: "A" },
  accessToken: "at",
  accessTokenExpiresAt: Math.floor(Date.now() / 1000) + 3600,
};

afterEach(() => {
  vi.clearAllMocks();
});

const call = (query: string) =>
  GET(new Request(`http://0.0.0.0:3000/api/session/refresh${query}`));

describe("GET /api/session/refresh", () => {
  it("renews the session, then goes back to the page", async () => {
    unstable_update.mockResolvedValue(FRESH);

    const response = await call("?callbackUrl=%2Fportal%2Fcalibration");

    expect(unstable_update).toHaveBeenCalledOnce();
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe("/portal/calibration");
  });

  it("redirects with a relative location, never the container's own address", async () => {
    // Behind the proxy the request URL is http://0.0.0.0:3000 (v1's README:
    // set AUTH_URL); a Location built from it would send the browser there.
    unstable_update.mockResolvedValue(FRESH);
    const response = await call("?callbackUrl=%2Fportal");
    expect(response.headers.get("location")).not.toContain("0.0.0.0");
  });

  it("sends the user to sign in when the refresh failed", async () => {
    unstable_update.mockResolvedValue({ ...FRESH, error: "RefreshAccessTokenError" });

    const response = await call("?callbackUrl=%2Fportal%2Ftriage");

    expect(response.headers.get("location")).toBe(
      "/api/auth/signin?callbackUrl=%2Fportal%2Ftriage",
    );
  });

  it("sends the user to sign in when the token is still not fresh (no loop)", async () => {
    unstable_update.mockResolvedValue({ ...FRESH, accessTokenExpiresAt: 1 });

    const response = await call("?callbackUrl=%2Fportal");

    expect(response.headers.get("location")).toMatch(/^\/api\/auth\/signin/);
  });

  it("sends a signed-out user to sign in", async () => {
    unstable_update.mockResolvedValue(null);
    const response = await call("?callbackUrl=%2Fportal");
    expect(response.headers.get("location")).toMatch(/^\/api\/auth\/signin/);
  });

  it("never redirects off the site", async () => {
    unstable_update.mockResolvedValue(FRESH);
    const response = await call("?callbackUrl=https%3A%2F%2Fevil.example%2F");
    expect(response.headers.get("location")).toBe("/portal");
  });
});
