// The owner, 2026-11-12: time to first byte for every page, kept to improve
// the site's performance (migration 0091).
import { describe, expect, it } from "vitest";
import { MAX_TIMINGS, navigationTarget, parseBeacon, routeTemplate } from "./page-timings";

describe("routeTemplate", () => {
  it.each([
    ["/", "/"],
    ["/lab/fish", "/[tenant]/fish"],
    ["/lab/fish/", "/[tenant]/fish"],
    ["/conservation-angler", "/[tenant]"],
    ["/lab/data/missions/383", "/[tenant]/data/missions/[n]"],
    [
      "/lab/data/missions/383/frames/3795f4b1-a77d-4f4f-a1fe-cd070041b000",
      "/[tenant]/data/missions/[n]/frames/[id]",
    ],
    [
      "/lab/fish/3795f4b1-a77d-4f4f-a1fe-cd070041b000/e0e12c19-873a-400c-b4ef-18001b4df483",
      "/[tenant]/fish/[id]/[id]",
    ],
    ["/portal/triage", "/portal/triage"],
    ["/tokens", "/tokens"],
    ["/admin/dataset", "/admin/dataset"],
  ])("%s is %s", (path, template) => {
    expect(routeTemplate(path)).toBe(template);
  });

  it("drops the query and the fragment", () => {
    expect(routeTemplate("/lab/fish?page=2&species=x#top")).toBe("/[tenant]/fish");
  });

  it("names a segment it can't place, so no one's text is kept", () => {
    expect(routeTemplate("/lab/data/captures/3795f4b1-a77d")).toBe("/[tenant]/data/captures/[segment]");
  });

  it("is never longer than the API takes", () => {
    expect(routeTemplate("/lab" + "/fish".repeat(100)).length).toBeLessThanOrEqual(200);
  });
});

describe("parseBeacon", () => {
  const ttfb = { route: "/[tenant]/fish", metric: "TTFB", value: 640.5, kind: "load" };

  it("keeps well-formed timings", () => {
    expect(parseBeacon([ttfb, { ...ttfb, metric: "NAV", kind: "navigation" }])).toEqual([
      ttfb,
      { ...ttfb, metric: "NAV", kind: "navigation" },
    ]);
  });

  it("re-templates a route rather than trusting the browser's", () => {
    expect(parseBeacon([{ ...ttfb, route: "/lab/data/missions/12" }])).toEqual([
      { ...ttfb, route: "/[tenant]/data/missions/[n]" },
    ]);
  });

  it.each([
    ["an unknown metric", { ...ttfb, metric: "BOGUS" }],
    ["a negative value", { ...ttfb, value: -1 }],
    ["a value over ten minutes", { ...ttfb, value: 600_001 }],
    ["a value that isn't a number", { ...ttfb, value: "12" }],
    ["NaN", { ...ttfb, value: Number.NaN }],
    ["an unknown kind", { ...ttfb, kind: "sideways" }],
    ["a route without a slash", { ...ttfb, route: "fish" }],
  ])("drops %s", (_, timing) => {
    expect(parseBeacon([ttfb, timing])).toEqual([ttfb]);
  });

  it("refuses what isn't a list, or an empty one", () => {
    expect(parseBeacon({ timings: [ttfb] })).toBeNull();
    expect(parseBeacon([])).toBeNull();
    expect(parseBeacon([{ ...ttfb, metric: "BOGUS" }])).toBeNull();
  });

  it(`keeps at most ${MAX_TIMINGS}`, () => {
    expect(parseBeacon(Array(MAX_TIMINGS + 10).fill(ttfb))).toHaveLength(MAX_TIMINGS);
  });
});

describe("navigationTarget", () => {
  const here = "https://fishsense.e4e.ucsd.edu/lab/fish";
  const click = { button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false };

  it("is the page a plain click on an in-app link opens", () => {
    expect(navigationTarget("/lab/data/missions", null, click, here)).toBe("/lab/data/missions");
  });

  it("counts a change of query, such as the next page", () => {
    expect(navigationTarget("/lab/fish?page=2", null, click, here)).toBe("/lab/fish?page=2");
  });

  it.each([
    ["another site", "https://example.org/x", null, click],
    ["a new tab", "/lab/data", "_blank", click],
    ["a modified click", "/lab/data", null, { ...click, metaKey: true }],
    ["a middle click", "/lab/data", null, { ...click, button: 1 }],
    ["the same page", "/lab/fish", null, click],
    ["a fragment on the same page", "/lab/fish#top", null, click],
    ["the API's own routes (sign-in, downloads)", "/api/auth/signin", null, click],
  ])("is nothing for %s", (_, href, target, event) => {
    expect(navigationTarget(href, target, event, here)).toBeNull();
  });
});
