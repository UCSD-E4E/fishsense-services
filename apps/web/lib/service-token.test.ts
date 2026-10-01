// New in v2. The landing page is public, so it has no user whose token could
// ask the API anything. v1 used a shared Basic-auth account; v2 mints a token
// for an Authentik service account that is a member of the tenant. Cached and
// deduplicated the same way lib/label-studio.ts treats Label Studio's token.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { clientCredentialsToken } = vi.hoisted(() => ({
  clientCredentialsToken: vi.fn(),
}));
vi.mock("./oidc", () => ({ clientCredentialsToken }));

import { __resetServiceToken, getServiceToken } from "./service-token";

const NOW_MS = Date.parse("2026-09-27T00:00:00Z");

beforeEach(() => {
  __resetServiceToken();
  clientCredentialsToken.mockReset();
  let n = 0;
  clientCredentialsToken.mockImplementation(async () => {
    n += 1;
    return { accessToken: `svc-${n}`, expiresAt: Date.now() / 1000 + 300 };
  });
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW_MS);
});

afterEach(() => {
  vi.useRealTimers();
  __resetServiceToken();
});

describe("getServiceToken", () => {
  it("mints once and reuses the token while it is fresh", async () => {
    expect(await getServiceToken()).toBe("svc-1");
    expect(await getServiceToken()).toBe("svc-1");
    expect(clientCredentialsToken).toHaveBeenCalledTimes(1);
  });

  it("shares one mint between concurrent callers", async () => {
    // The landing page asks for four kinds at once; four token requests per
    // render would be the stampede lib/label-studio.ts already learned from.
    const tokens = await Promise.all([1, 2, 3, 4].map(() => getServiceToken()));
    expect(tokens).toEqual(["svc-1", "svc-1", "svc-1", "svc-1"]);
    expect(clientCredentialsToken).toHaveBeenCalledTimes(1);
  });

  it("re-mints shortly before the token expires, not after", async () => {
    await getServiceToken();
    vi.setSystemTime(NOW_MS + 269_000); // 31 s left
    expect(await getServiceToken()).toBe("svc-1");
    vi.setSystemTime(NOW_MS + 271_000); // 29 s left: inside the skew
    expect(await getServiceToken()).toBe("svc-2");
  });

  it("re-mints when forced", async () => {
    await getServiceToken();
    expect(await getServiceToken(true)).toBe("svc-2");
  });

  it("does not cache a failure", async () => {
    clientCredentialsToken.mockRejectedValueOnce(new Error("idp down"));
    await expect(getServiceToken()).rejects.toThrow(/idp down/);
    expect(await getServiceToken()).toBe("svc-1");
  });
});
