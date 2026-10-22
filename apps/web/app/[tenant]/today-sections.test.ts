// Today's sections, each streamed on its own (app/[tenant]/page.tsx).
//
// Pinned here:
//  * each section reads only what it shows (three fish, not a page of fifty);
//  * a section whose call fails says so, as an alert, and claims nothing:
//    "Needs you" never shows its empty state for a summary it could not read,
//    and the labeling never silently loses its dashboards (review of #250);
//  * the labeling is shown to staff and the dashboards to system admins.
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NeedsYou, Numbers, RecentFish, RecentTrips, WhereMeasured, Work } from "./today-sections";

vi.mock("@/app/_shell/work-sections", () => ({
  WorkSections: ({ dashboards }: { dashboards: boolean }) =>
    createElement("p", null, dashboards ? "work and dashboards" : "work"),
}));

beforeEach(() => {
  vi.stubEnv("FISHSENSE_API_URL", "http://api.test");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

type FetchSig = (input: Request, init?: RequestInit) => Promise<Response>;

const SUMMARY = {
  lengths: 3314,
  wild_fish: 203,
  species: 11,
  missions: 525,
  deployments: 36,
  frames: 69162,
  unknown_ports: 2,
  latest_activity: "2026-10-05T23:02:49Z",
};

/** The API, answering each path from `routes`; anything else is a 500. */
function api(routes: Record<string, unknown>) {
  const fetchMock = vi.fn<FetchSig>(async (request) => {
    const path = new URL(request.url).pathname;
    if (!(path in routes)) return new Response("down", { status: 500 });
    return new Response(JSON.stringify(routes[path]), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function html(section: Promise<ReactElement | null>): Promise<string> {
  const element = await section;
  return element === null ? "" : renderToStaticMarkup(element);
}

const SECTION = { token: "t", slug: "lab" };

describe("Numbers", () => {
  it("counts wild fish, and notes the fish models apart", async () => {
    api({ "/tenants/lab/summary": SUMMARY });

    const out = await html(Numbers(SECTION));

    expect(out).toContain("Fish measured");
    expect(out).toMatch(/>203</);
    expect(out).toContain("11 species");
    expect(out).toContain("69,162");
    expect(out).toContain("3,111 fish-model lengths in pool trials");
    expect(out).not.toContain("3,314");
  });

  it("says nothing of fish models when there are none", async () => {
    api({ "/tenants/lab/summary": { ...SUMMARY, lengths: 203 } });

    expect(await html(Numbers(SECTION))).not.toContain("fish-model");
  });

  it("says it could not load them", async () => {
    api({});

    const out = await html(Numbers(SECTION));

    expect(out).toContain('role="alert"');
    expect(out).toContain("Could not load the numbers");
  });
});

describe("NeedsYou", () => {
  it("counts the missions with an unknown port", async () => {
    api({ "/tenants/lab/summary": SUMMARY });

    const out = await html(NeedsYou(SECTION));

    expect(out).toContain("Missions with an unknown port");
    expect(out).toContain(">2<");
  });

  it("never says there is nothing to do when it could not look", async () => {
    api({});

    const out = await html(NeedsYou(SECTION));

    expect(out).toContain('role="alert"');
    expect(out).not.toContain("Nothing yet");
  });
});

describe("RecentFish", () => {
  it("asks for the three it shows", async () => {
    const fetchMock = api({ "/tenants/lab/observations": { observations: [], next: null } });

    const out = await html(RecentFish(SECTION));

    expect(fetchMock.mock.calls[0][0].url).toBe("http://api.test/tenants/lab/observations?limit=3");
    expect(out).toContain("No measured fish yet.");
  });

  it("says it could not load them", async () => {
    api({});

    expect(await html(RecentFish(SECTION))).toContain("Could not load the recent fish");
  });
});

describe("RecentTrips", () => {
  it("lists the newest deployments", async () => {
    api({
      "/tenants/lab/deployments": [
        { number: 1, name: "Bonaire 2025", source_path: null, started_on: "2025-01-10",
          ended_on: null, notes: null, readme: null, missions: 4 },
      ],
    });

    const out = await html(RecentTrips(SECTION));

    expect(out).toContain("Bonaire 2025");
    expect(out).toContain("4 missions");
  });

  it("says it could not load them", async () => {
    api({});

    expect(await html(RecentTrips(SECTION))).toContain("Could not load the deployments");
  });
});

describe("Work", () => {
  it("shows staff the labeling", async () => {
    api({ "/me": { is_system_admin: false } });

    const out = await html(Work({ token: "t", staff: true }));

    expect(out).toContain("Labeling");
    expect(out).toContain("<p>work</p>");
  });

  it("shows a system admin the dashboards too", async () => {
    api({ "/me": { is_system_admin: true } });

    const out = await html(Work({ token: "t", staff: false }));

    expect(out).toContain("Labeling and dashboards");
    expect(out).toContain("work and dashboards");
  });

  it("shows anyone else nothing", async () => {
    api({ "/me": { is_system_admin: false } });

    expect(await html(Work({ token: "t", staff: false }))).toBe("");
  });

  it("says when it could not tell who may see the dashboards", async () => {
    api({});

    const staff = await html(Work({ token: "t", staff: true }));
    const member = await html(Work({ token: "t", staff: false }));

    expect(staff).toContain('role="alert"');
    expect(staff).toContain("<p>work</p>");
    expect(member).toContain('role="alert"');
  });
});

describe("WhereMeasured", () => {
  const regions = {
    regions: [
      { name: "Florida Keys, USA", latitude: 27.0, longitude: -81.5, fish: 2101 },
      { name: "Southern California, USA", latitude: 33.5, longitude: -117.5, fish: 1205 },
    ],
    elsewhere: 8,
  };

  it("puts each region on the map with its fish", async () => {
    api({ "/tenants/lab/regions": regions });

    const out = await html(WhereMeasured(SECTION));

    expect(out).toContain("Florida Keys, USA");
    expect(out).toContain("2,101 fish");
    expect(out).toContain("1,205 fish");
    expect(out).toContain("world.svg");
    expect(out).toContain("8 more fish without a region");
  });

  it("says where measurements will show before there are any", async () => {
    api({ "/tenants/lab/regions": { regions: [], elsewhere: 0 } });

    expect(await html(WhereMeasured(SECTION))).toContain("Measured fish will appear here by region");
  });

  it("says it could not load the map", async () => {
    api({});

    const out = await html(WhereMeasured(SECTION));

    expect(out).toContain('role="alert"');
    expect(out).toContain("Could not load the map");
  });
});
