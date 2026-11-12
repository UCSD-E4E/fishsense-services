"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { useReportWebVitals } from "next/web-vitals";
import { useEffect } from "react";
import {
  MAX_TIMINGS,
  METRICS,
  navigationTarget,
  routeTemplate,
  type Kind,
  type Metric,
  type PageTiming,
} from "@/lib/page-timings";

/** How often waiting timings are sent, and how long a click may wait for its
 *  page before it no longer counts (a click that opened nothing). */
const FLUSH_MS = 5_000;
const CLICK_EXPIRES_MS = 60_000;

const queue: PageTiming[] = [];
let click: { opens: string; at: number } | null = null;
/** The page the browser loaded: LCP, CLS and INP end when it is hidden, which
 *  may be after clicks have moved on to other pages. */
let loaded: string | null = null;

function enqueue(route: string, metric: Metric, value: number, kind: Kind) {
  if (queue.length < MAX_TIMINGS) queue.push({ route: routeTemplate(route), metric, value, kind });
}

function flush() {
  if (queue.length === 0) return;
  const body = JSON.stringify(queue.splice(0, MAX_TIMINGS));
  try {
    // A beacon survives the page closing, which is when LCP, CLS and INP end.
    if (!navigator.sendBeacon?.("/api/vitals", new Blob([body], { type: "application/json" }))) {
      void fetch("/api/vitals", { method: "POST", body, keepalive: true }).catch(() => {});
    }
  } catch {
    // A timing is never worth an error.
  }
}

/** One function for the page's life: `useReportWebVitals` subscribes again
 *  whenever it is handed a new one, and web-vitals then reports the final
 *  TTFB and FCP a second time (seen in a local run, 2026-11-12). */
function onLoadMetric(metric: { name: string; value: number }) {
  if ((METRICS as readonly string[]).includes(metric.name)) {
    enqueue(loaded ?? window.location.pathname, metric.name as Metric, metric.value, "load");
  }
}

function onClick(event: MouseEvent) {
  const link = (event.target as Element | null)?.closest?.("a[href]");
  if (!(link instanceof HTMLAnchorElement) || event.defaultPrevented) return;
  const opens = navigationTarget(
    link.getAttribute("href") ?? "",
    link.getAttribute("target"),
    event,
    window.location.href,
  );
  click = opens ? { opens, at: performance.now() } : null;
}

/**
 * Measures every page (lib/page-timings.ts): on a load, time to first byte
 * and the Core Web Vitals; on a click within the app, the time until the next
 * page has painted (`NAV`). Sends them every few seconds and when the page is
 * hidden. Renders nothing.
 */
export function PageTimings() {
  const pathname = usePathname();
  const search = useSearchParams().toString();

  useReportWebVitals(onLoadMetric);

  useEffect(() => {
    loaded ??= window.location.pathname;
    document.addEventListener("click", onClick, { capture: true });
    const hidden = () => document.visibilityState === "hidden" && flush();
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", flush);
    const timer = window.setInterval(flush, FLUSH_MS);
    return () => {
      document.removeEventListener("click", onClick, { capture: true });
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", flush);
      window.clearInterval(timer);
    };
  }, []);

  // The new page is in the document; it is on screen at the next frame.
  useEffect(() => {
    const pending = click;
    click = null;
    if (!pending || performance.now() - pending.at > CLICK_EXPIRES_MS) return;
    const frame = requestAnimationFrame(() => {
      // A redirect can land elsewhere than the link said: time the page shown.
      enqueue(pathname, "NAV", performance.now() - pending.at, "navigation");
    });
    return () => cancelAnimationFrame(frame);
  }, [pathname, search]);

  return null;
}
