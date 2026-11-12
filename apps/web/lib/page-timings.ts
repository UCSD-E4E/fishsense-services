/**
 * How long the web's pages take, as their visitors' browsers measured it.
 *
 * New in v2 (the owner, 2026-11-12: "things are so slow, can we add time to
 * first byte measurements for every page? ... it would be nice to log this
 * information for trying to improve the performance of the website"). The
 * browser measures (`app/_shell/page-timings.tsx`), `/api/vitals` passes the
 * timings to the API as the web's service account, and the API keeps them in
 * `page_timings` (migration 0091) for 90 days. Two kinds:
 *
 *  * `load`: a page opened from its address (a link from elsewhere, a
 *    reload): time to first byte, the server's whole share, and the Core Web
 *    Vitals (FCP, LCP, INP, CLS), from Next's `useReportWebVitals`;
 *  * `navigation`: a click on a link within the app. Next fetches the next
 *    page's server render and shows it without a load, so a browser reports
 *    no TTFB for it; `NAV` is the click-to-new-page time instead, which is what
 *    a person waits through.
 *
 * Routes are kept as templates (`/[tenant]/fish/[id]/[n]`), so ids don't split
 * one page into thousands and no path a person typed is stored. This module is
 * the rules, shared by the browser and the server, which re-checks every
 * timing; the API checks them a third time.
 */
import { isReserved } from "./tenant-paths";

export const METRICS = ["TTFB", "FCP", "LCP", "INP", "CLS", "NAV"] as const;
export type Metric = (typeof METRICS)[number];
export const KINDS = ["load", "navigation"] as const;
export type Kind = (typeof KINDS)[number];

export type PageTiming = { route: string; metric: Metric; value: number; kind: Kind };

/** The most one beacon carries (the API's `MAX_TIMINGS`). */
export const MAX_TIMINGS = 50;
/** Ten minutes in milliseconds: anything longer is no page's (the API's). */
const MAX_VALUE = 600_000;
const MAX_ROUTE = 200;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const NUMBER = /^\d+$/;
/** A segment that names a page, not data: lowercase words. */
const WORD = /^[a-z][a-z-]*$/;

/**
 * The page's template: its tenant `[tenant]`, uuids `[id]`, numbers `[n]`,
 * and any other segment after the first that isn't a plain word `[segment]`.
 */
export function routeTemplate(path: string): string {
  const pathname = path.split(/[?#]/, 1)[0];
  const segments = pathname.split("/").filter(Boolean);
  const named = segments.map((segment, i) => {
    if (i === 0 && !isReserved(segment)) return "[tenant]";
    if (UUID.test(segment)) return "[id]";
    if (NUMBER.test(segment)) return "[n]";
    return WORD.test(segment) ? segment : "[segment]";
  });
  return `/${named.join("/")}`.slice(0, MAX_ROUTE);
}

function timing(raw: unknown): PageTiming | null {
  if (!raw || typeof raw !== "object") return null;
  const { route, metric, value, kind } = raw as Record<string, unknown>;
  if (typeof route !== "string" || !route.startsWith("/")) return null;
  if (!(METRICS as readonly unknown[]).includes(metric)) return null;
  if (!(KINDS as readonly unknown[]).includes(kind)) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  if (value < 0 || value > MAX_VALUE) return null;
  return { route: routeTemplate(route), metric: metric as Metric, value, kind: kind as Kind };
}

/** A beacon's well-formed timings, at most `MAX_TIMINGS`; null if none. */
export function parseBeacon(body: unknown): PageTiming[] | null {
  if (!Array.isArray(body)) return null;
  const kept = body
    .map(timing)
    .filter((t): t is PageTiming => t !== null)
    .slice(0, MAX_TIMINGS);
  return kept.length > 0 ? kept : null;
}

type Click = {
  button: number;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
};

/**
 * The path and query a click opens within the app, which Next will fetch and
 * show (`NAV`); null for anything Next leaves to the browser or that changes
 * no page: another site, a new tab, a modified or middle click, the same page
 * or a fragment of it, and `/api/...` (sign-in, downloads).
 */
export function navigationTarget(
  href: string,
  target: string | null,
  click: Click,
  here: string,
): string | null {
  if (click.button !== 0 || click.metaKey || click.ctrlKey || click.shiftKey || click.altKey) {
    return null;
  }
  if (target && target !== "_self") return null;
  const current = new URL(here);
  const next = new URL(href, current);
  if (next.origin !== current.origin) return null;
  if (next.pathname.startsWith("/api/")) return null;
  const opens = `${next.pathname}${next.search}`;
  if (opens === `${current.pathname}${current.search}`) return null;
  return opens;
}
