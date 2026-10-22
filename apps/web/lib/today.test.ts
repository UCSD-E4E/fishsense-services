import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { project } from "./public-page";
import { bubbleSize, getTenantRegions, mapFrame, MAP_ASPECT, recentDeployments } from "./today";
import type { Deployment } from "./mission-facts";

const d = (number: number, started_on: string | null) =>
  ({ number, started_on, name: `d${number}`, missions: 1 }) as unknown as Deployment;

describe("Today's recent deployments", () => {
  it("are the newest trips first, undated last, at most n", () => {
    const list = [d(1, "2023-08-03"), d(2, null), d(3, "2025-01-17"), d(4, "2024-12-10")];

    expect(recentDeployments(list, 3).map((x) => x.number)).toEqual([3, 4, 1]);
    expect(recentDeployments([d(2, null), d(5, null)], 3).map((x) => x.number)).toEqual([5, 2]);
  });
});

// Where the tenant's fish were measured (the Today map). For a picture to
// screenshot, not a survey (the owner, 2026-10-21): the world outline framed
// to the tenant's regions, a bubble per region sized by its fish.

const KEYS = { name: "Florida Keys, USA", latitude: 27.0, longitude: -81.5, fish: 2101 };
const SOCAL = { name: "Southern California, USA", latitude: 33.5, longitude: -117.5, fish: 1205 };
const CARIBBEAN = { name: "Caribbean", latitude: 18.5, longitude: -76.0, fish: 7 };

/** Where a region falls inside the frame, from 0 to 1 each way. */
function inside(frame: ReturnType<typeof mapFrame>, r: { latitude: number; longitude: number }) {
  const { left, top } = project(r.latitude, r.longitude);
  return { x: (left / 100 - frame.x) / frame.width, y: (top / 100 - frame.y) / frame.height };
}

describe("mapFrame", () => {
  it("frames every region with room around it, the map's shape kept", () => {
    const regions = [KEYS, SOCAL, CARIBBEAN];
    const frame = mapFrame(regions);

    for (const r of regions) {
      const { x, y } = inside(frame, r);
      expect(x).toBeGreaterThan(0.1);
      expect(x).toBeLessThan(0.9);
      expect(y).toBeGreaterThan(0.1);
      expect(y).toBeLessThan(0.9);
    }
    expect(frame.aspect).toBeCloseTo(MAP_ASPECT);
  });

  it("shows a lone region in its part of the world, not a blur of coast", () => {
    const frame = mapFrame([SOCAL]);

    expect(frame.width).toBeGreaterThanOrEqual(0.3);
    const { x, y } = inside(frame, SOCAL);
    expect(x).toBeCloseTo(0.5, 1);
    expect(y).toBeCloseTo(0.5, 1);
  });

  it("stays on the map at its edges", () => {
    const frame = mapFrame([{ latitude: 80, longitude: 179 }]);

    expect(frame.x).toBeGreaterThanOrEqual(0);
    expect(frame.y).toBeGreaterThanOrEqual(0);
    expect(frame.x + frame.width).toBeLessThanOrEqual(1 + 1e-9);
    expect(frame.y + frame.height).toBeLessThanOrEqual(1 + 1e-9);
  });
});

describe("bubbleSize", () => {
  it("grows with the fish, by area, from a size that can be read", () => {
    expect(bubbleSize(2101, 2101)).toBe(120);
    expect(bubbleSize(7, 2101)).toBeGreaterThanOrEqual(40);
    expect(bubbleSize(1205, 2101)).toBeGreaterThan(bubbleSize(7, 2101));
    expect(bubbleSize(1205, 2101)).toBeLessThan(120);
  });
});

describe("getTenantRegions", () => {
  beforeEach(() => vi.stubEnv("FISHSENSE_API_URL", "http://api.test"));
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("asks the API, as the signed-in user", async () => {
    const fetchMock = vi.fn(
      async (_request: Request) =>
        new Response(JSON.stringify({ regions: [SOCAL], elsewhere: 0 }), {
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const found = await getTenantRegions("t", "lab");

    expect(fetchMock.mock.calls[0][0].url).toBe("http://api.test/tenants/lab/regions");
    expect(fetchMock.mock.calls[0][0].headers.get("authorization")).toBe("Bearer t");
    expect(found.regions).toEqual([SOCAL]);
  });
});
