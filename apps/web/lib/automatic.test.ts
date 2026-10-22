// The automatic track, as the web shows it for review (the owner,
// 2026-10-21): read-only, for any member, marked automatic and unvalidated.
//
// Pinned here:
//  * a frame or mission the run never reached (or the caller can't see) is
//    null, not an error page; ids from the URL are checked before a request;
//  * every status reads as words a reviewer understands;
//  * the overlay is drawn only on the render the points belong to, at its size;
//  * agreement with the human lengths reads as agreement, with its n.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  agreement,
  automaticOverlay,
  getAutomaticFrame,
  getMissionAutomatic,
  statusLabel,
  type AutomaticFrame,
  type MissionAutomatic,
} from "./automatic";

type FetchSig = (input: Request, init?: RequestInit) => Promise<Response>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const CAPTURE = "11111111-2222-3333-4444-555555555555";

beforeEach(() => {
  vi.stubEnv("FISHSENSE_API_URL", "http://api.test");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("getAutomaticFrame", () => {
  it("asks for one frame's automatic result as the signed-in user", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse({ status: "predicted" }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getAutomaticFrame("user-token", "lab", CAPTURE)).toEqual({ status: "predicted" });
    const [request] = fetchMock.mock.calls[0];
    expect(request.url).toBe(`http://api.test/tenants/lab/captures/${CAPTURE}/automatic`);
    expect(request.headers.get("authorization")).toBe("Bearer user-token");
  });

  it("is null for a frame the run never reached", async () => {
    vi.stubGlobal("fetch", vi.fn<FetchSig>(async () => jsonResponse({ detail: "x" }, 404)));

    expect(await getAutomaticFrame("t", "lab", CAPTURE)).toBeNull();
  });

  it("refuses an id that isn't one before any request", async () => {
    vi.stubGlobal("fetch", vi.fn());

    await expect(getAutomaticFrame("t", "lab", "../x")).rejects.toThrow(/capture/);
  });
});

describe("getMissionAutomatic", () => {
  it("asks for one mission's run, and is null for an unknown one", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse({ detail: "x" }, 404));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getMissionAutomatic("t", "lab", 383)).toBeNull();
    expect(fetchMock.mock.calls[0][0].url).toBe("http://api.test/tenants/lab/missions/383/automatic");
  });
});

describe("statusLabel", () => {
  it("reads each status as words", () => {
    expect(statusLabel("predicted")).toBe("Head and tail found");
    expect(statusLabel("no_laser_dot")).toBe("No laser dot found");
    expect(statusLabel("no_detections")).toBe("No fish found");
    expect(statusLabel("laser_off_all_fish")).toBe("Laser dot not on a fish");
    expect(statusLabel("slate_frame")).toBe("A slate frame");
  });

  it("shows a status it doesn't know as it is", () => {
    expect(statusLabel("something_new")).toBe("something_new");
  });
});

function frame(overrides: Partial<AutomaticFrame> = {}): AutomaticFrame {
  return {
    status: "predicted",
    render_url: "https://s3.test/r.JPG",
    render_width: 4014,
    render_height: 3016,
    head: { x: 1800, y: 1500 },
    tail: { x: 2300, y: 1480 },
    laser: { x: 2000, y: 1500 },
    mask_url: null,
    ...overrides,
  } as AutomaticFrame;
}

describe("automaticOverlay", () => {
  it("draws the points on the render they belong to, at its size", () => {
    expect(automaticOverlay(frame())).toEqual({
      width: 4014,
      height: 3016,
      head: { x: 1800, y: 1500 },
      tail: { x: 2300, y: 1480 },
      laser: { x: 2000, y: 1500 },
      maskUrl: null,
    });
  });

  it("draws nothing without the render", () => {
    expect(automaticOverlay(frame({ render_url: null }))).toBeNull();
    expect(automaticOverlay(frame({ render_width: null }))).toBeNull();
  });
});

describe("agreement", () => {
  it("reads as agreement with the human lengths, with its n", () => {
    const run = { compared: 12, median_error_pct: 3.214, within_5_pct: 9, within_10_pct: 11 } as MissionAutomatic;

    expect(agreement(run)).toBe("Median +3.2% against the human length, 9 of 12 within 5%, 11 within 10%");
  });

  it("says when there is nothing to compare", () => {
    expect(agreement({ compared: 0 } as MissionAutomatic)).toBe("No frame has both an automatic and a human length yet");
  });
});
