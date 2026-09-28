// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/label-studio-limiter.test.ts.
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MAX_CONCURRENT,
  __resetLimiter,
  lsFetch,
  retryAfterMs,
} from "./label-studio-limiter";

/**
 * Hosted Label Studio rate-limits per account, so the budget is shared by
 * every request this app makes — the landing page's project fan-out, the
 * triage queue's task scan, the image proxy, the token refresh, and the
 * accept POST all draw on one pool.
 *
 * Nothing enforced that. `getProjects` fanned out over every project id with
 * `Promise.allSettled`, which with one LS project per dive is a burst of
 * dozens of simultaneous requests; each 429 was then retried independently,
 * so a rate-limited load turned into several times as many requests as the
 * load that caused it. Observed in prod 2026-09-07: 20+ consecutive
 * `GET /api/projects/{id}` 429s in the web container log.
 */

function ok(): Response {
  return new Response("{}", { status: 200 });
}

function rateLimited(retryAfter?: string): Response {
  return new Response("{}", {
    status: 429,
    headers: retryAfter ? { "retry-after": retryAfter } : undefined,
  });
}

afterEach(() => {
  __resetLimiter();
  vi.restoreAllMocks();
});

describe("concurrency", () => {
  it("never has more than MAX_CONCURRENT requests in flight", async () => {
    let inFlight = 0;
    let peak = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        await new Promise((r) => setTimeout(r, 5));
        inFlight -= 1;
        return ok();
      }),
    );

    await Promise.all(
      Array.from({ length: MAX_CONCURRENT * 5 }, (_, i) => lsFetch(`/p/${i}`)),
    );

    expect(peak).toBeLessThanOrEqual(MAX_CONCURRENT);
    expect(peak).toBeGreaterThan(1); // still parallel, just bounded
  });
});

describe("rate limiting", () => {
  it("retries a 429 and returns the eventual success", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(rateLimited("0"))
      .mockResolvedValueOnce(ok());
    vi.stubGlobal("fetch", fetchMock);

    const response = await lsFetch("/p/1");

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("gives up and returns the 429 rather than retrying forever", async () => {
    const fetchMock = vi.fn(async () => rateLimited("0"));
    vi.stubGlobal("fetch", fetchMock);

    const response = await lsFetch("/p/1");

    expect(response.status).toBe(429);
    // The retry budget is finite: an unbounded loop against a rate limiter is
    // how a slow page becomes an outage.
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(5);
  });

  /**
   * The one that matters. Independent per-request backoff means a burst of 30
   * requests answers a single rate-limit window with 30 more requests. One
   * 429 has to slow down everything, not just the caller that saw it.
   */
  it("makes one 429 pause requests that have not been sent yet", async () => {
    const started: number[] = [];
    let first = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        started.push(Date.now());
        if (first) {
          first = false;
          return rateLimited("0.2");
        }
        return ok();
      }),
    );

    const begin = Date.now();
    await lsFetch("/p/1");
    const later = await lsFetch("/p/2");

    expect(later.status).toBe(200);
    // The second call was issued after the cooldown the FIRST call learned
    // about, without having seen a 429 itself.
    expect(started[started.length - 1] - begin).toBeGreaterThanOrEqual(180);
  });
});

describe("retryAfterMs", () => {
  it("honours a numeric Retry-After in seconds", () => {
    expect(retryAfterMs(rateLimited("2"), 0)).toBe(2000);
  });

  it("falls back to exponential backoff when the server says nothing", () => {
    const first = retryAfterMs(rateLimited(), 0);
    const second = retryAfterMs(rateLimited(), 1);
    expect(second).toBeGreaterThan(first);
  });
});
