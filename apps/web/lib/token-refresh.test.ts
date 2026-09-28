// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/token-refresh.test.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetTokenCache, getAccessToken } from "./label-studio";

beforeEach(() => {
  vi.stubEnv("LABEL_STUDIO_URL", "http://ls.test");
  vi.stubEnv("LABEL_STUDIO_API_KEY", "pat");
  __resetTokenCache();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function jwt(ttlSeconds: number): string {
  const payload = Buffer.from(
    JSON.stringify({ exp: Math.floor(Date.now() / 1000) + ttlSeconds }),
  ).toString("base64url");
  return `h.${payload}.s`;
}

describe("token refresh under rate limiting", () => {
  // The refresh endpoint was the ONE request with no backoff: the retry logic
  // lived in `authed`, which wraps resource calls and never sees this one. A
  // 429 here failed the whole action — surfacing as "Accept failed: token
  // refresh failed: 429" on a frame the labeler had already judged.
  it("retries a 429 and returns the token", async () => {
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        calls += 1;
        return calls === 1
          ? new Response("slow down", { status: 429, headers: { "retry-after": "0" } })
          : new Response(JSON.stringify({ access: jwt(300) }), { status: 200 });
      }),
    );

    await expect(getAccessToken()).resolves.toMatch(/^h\./);
    expect(calls).toBe(2);
  });

  it("gives up after a bounded number of retries", async () => {
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        calls += 1;
        return new Response("slow down", { status: 429, headers: { "retry-after": "0" } });
      }),
    );

    await expect(getAccessToken()).rejects.toThrow(/429/);
    expect(calls).toBeGreaterThan(1);
    expect(calls).toBeLessThanOrEqual(5);
  });
});

describe("concurrent refreshes", () => {
  // A burst of FORCED refreshes each fired its own POST, so one expired token
  // became N simultaneous refreshes — the stampede that earns the rate limit
  // in the first place.
  it("collapses concurrent forced refreshes into one request", async () => {
    const fetchMock = vi.fn(async () => {
      await new Promise((r) => setTimeout(r, 10));
      return new Response(JSON.stringify({ access: jwt(300) }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const tokens = await Promise.all([
      getAccessToken(true),
      getAccessToken(true),
      getAccessToken(true),
      getAccessToken(true),
    ]);

    expect(new Set(tokens).size).toBe(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
