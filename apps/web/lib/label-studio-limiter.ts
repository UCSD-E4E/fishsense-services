// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/label-studio-limiter.ts.
/**
 * One shared throttle in front of every Label Studio request.
 *
 * Hosted Label Studio rate-limits per account, so the budget belongs to the
 * app, not to any one caller. The landing page's project fan-out, the triage
 * queue's task scan, the image proxy, the token refresh and the accept POST
 * all spend from the same pool — and until this existed, none of them knew
 * the others were spending.
 *
 * Two faults followed from that, and the second is the one that made a busy
 * moment self-sustaining:
 *
 *  * `getProjects` fetched every project id at once with `Promise.allSettled`.
 *    There is one LS project per dive, so that is a burst of dozens of
 *    simultaneous requests every time the cache is cold. Prod 2026-09-07: 20+
 *    consecutive `GET /api/projects/{id}` 429s in the web container log.
 *  * Each 429 was then retried by its own caller, independently. A burst of 30
 *    requests answered a single rate-limit window with 30 more, so the retry
 *    traffic was several times the traffic that earned the limit.
 *
 * So this bounds concurrency AND shares what it learns: a 429 seen by one
 * request holds back every request that has not been sent yet. Retrying is
 * centralised here too, because four call sites with four private backoff
 * loops is exactly how the amplification got in.
 */

/**
 * Bounded, not serialised. One at a time would make a 60-project landing page
 * unusable; the goal is to stop the thundering herd, not the parallelism.
 */
export const MAX_CONCURRENT = 4;

/** Finite on purpose: an unbounded retry loop against a rate limiter is how a
 *  slow page becomes an outage. */
export const MAX_RATE_LIMIT_RETRIES = 3;

const BACKOFF_BASE_MS = 750;

let active = 0;
const waiting: (() => void)[] = [];

/** Shared cooldown. Set by whoever sees a 429; obeyed by everyone. */
let cooldownUntil = 0;

/** Seconds Label Studio asked us to wait, or an exponential fallback. */
export function retryAfterMs(response: Response, attempt: number): number {
  const header = response.headers.get("retry-after");
  if (header) {
    const seconds = Number(header);
    if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
    const at = Date.parse(header);
    if (Number.isFinite(at)) return Math.max(0, at - Date.now());
  }
  return BACKOFF_BASE_MS * 2 ** attempt;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function acquire(): Promise<void> {
  if (active < MAX_CONCURRENT) {
    active += 1;
    return;
  }
  await new Promise<void>((resolve) => waiting.push(resolve));
  active += 1;
}

function release(): void {
  active -= 1;
  waiting.shift()?.();
}

/** Wait out any cooldown *before* taking a slot, so sleepers do not hold one. */
async function waitForCooldown(): Promise<void> {
  for (let remaining = cooldownUntil - Date.now(); remaining > 0; ) {
    await sleep(remaining);
    remaining = cooldownUntil - Date.now();
  }
}

/**
 * Fetch through the shared throttle, retrying rate limits.
 *
 * Returns the final 429 rather than throwing once the retry budget is spent —
 * callers already distinguish status codes, and throwing here would turn a
 * transient limit into an error indistinguishable from a dead project id.
 */
export async function lsFetch(url: string, init: RequestInit = {}): Promise<Response> {
  for (let attempt = 0; ; attempt += 1) {
    await waitForCooldown();

    await acquire();
    let response: Response;
    try {
      response = await fetch(url, init);
    } finally {
      release();
    }

    if (response.status !== 429) return response;

    const backoff = retryAfterMs(response, attempt);
    // Everyone waits, not just this caller. `max` so a longer window already
    // learned by another request is not shortened by this one.
    cooldownUntil = Math.max(cooldownUntil, Date.now() + backoff);

    if (attempt >= MAX_RATE_LIMIT_RETRIES) return response;
  }
}

/** Test seam — clears the shared cooldown and the queue between cases. */
export function __resetLimiter(): void {
  active = 0;
  waiting.length = 0;
  cooldownUntil = 0;
}
