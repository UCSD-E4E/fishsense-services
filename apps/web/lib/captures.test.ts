// FishSense Mobile captures, as the web shows them (new in v2).
//
// Pinned here:
//  * every call is made as the signed-in user, in a tenant they belong to --
//    a partner sees their own org's captures, never the lab's (the API
//    enforces it; the web only ever asks for the caller's own tenants);
//  * a tenant slug and a capture id from the URL are validated before they
//    go into an API path (request forgery);
//  * a missing capture is `null`, not an error page.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  chooseTenant,
  formatLength,
  getCapture,
  getCapturePage,
  measurementOverlay,
  osmLink,
} from "./captures";
import { ApiError } from "./api/client";

beforeEach(() => {
  vi.stubEnv("FISHSENSE_API_URL", "http://api.test");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

type FetchSig = (input: Request, init?: RequestInit) => Promise<Response>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const TENANTS = [
  {
    slug: "conservation-angler",
    name: "Conservation Angler",
    role: "member",
    isAdmin: false,
  },
  { slug: "lab", name: "Lab", role: "admin", isAdmin: true },
];
const CAPTURE = "6f1c8a52-9a4e-4c43-9b8e-4f4e2d1a0b11";

describe("chooseTenant", () => {
  it("takes the requested tenant when the user belongs to it", () => {
    expect(chooseTenant(TENANTS, "lab")?.slug).toBe("lab");
  });

  it("falls back to the first tenant for an absent or foreign request", () => {
    expect(chooseTenant(TENANTS, undefined)?.slug).toBe("conservation-angler");
    expect(chooseTenant(TENANTS, "someone-elses")?.slug).toBe(
      "conservation-angler",
    );
  });

  it("is null for a user in no tenant", () => {
    expect(chooseTenant([], "lab")).toBeNull();
  });
});

describe("getCapturePage", () => {
  it("lists a tenant's captures as the signed-in user", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      jsonResponse({ captures: [], next: null }),
    );
    vi.stubGlobal("fetch", fetchMock);

    expect(await getCapturePage("user-token", "conservation-angler")).toEqual({
      captures: [],
      next: null,
    });

    const [request, init] = fetchMock.mock.calls[0];
    expect(request.url).toBe(
      "http://api.test/tenants/conservation-angler/mobile/captures",
    );
    expect(request.headers.get("Authorization")).toBe("Bearer user-token");
    expect(request.cache === "no-store" || init?.cache === "no-store").toBe(
      true,
    );
  });

  it("passes the page cursor through", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      jsonResponse({ captures: [], next: null }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getCapturePage("t", "lab", "2026-10-11T17:00:00+00:00~" + CAPTURE);

    const url = new URL(fetchMock.mock.calls[0][0].url);
    expect(url.searchParams.get("before")).toBe(
      "2026-10-11T17:00:00+00:00~" + CAPTURE,
    );
  });

  it("refuses a tenant slug that could steer the request", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(getCapturePage("t", "../admin")).rejects.toThrow(/tenant/);
  });

  it("throws the API's refusal", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchSig>(async () => jsonResponse({ detail: "down" }, 503)),
    );
    const error = await getCapturePage("t", "lab").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(503);
  });
});

describe("getCapture", () => {
  it("reads one capture", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      jsonResponse({ client_capture_uuid: CAPTURE }),
    );
    vi.stubGlobal("fetch", fetchMock);

    expect((await getCapture("t", "lab", CAPTURE))?.client_capture_uuid).toBe(
      CAPTURE,
    );
    expect(fetchMock.mock.calls[0][0].url).toBe(
      `http://api.test/tenants/lab/mobile/captures/${CAPTURE}`,
    );
  });

  it("is null when there is no such capture (or it isn't the caller's tenant)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchSig>(async () => jsonResponse({ detail: "Not Found" }, 404)),
    );
    expect(await getCapture("t", "lab", CAPTURE)).toBeNull();
  });

  it("refuses a capture id that is not a uuid", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(getCapture("t", "lab", "x/../../dives")).rejects.toThrow(
      /capture/,
    );
  });
});

describe("formatLength", () => {
  it("shows centimetres to one place", () => {
    expect(formatLength(0.4237)).toBe("42.4 cm");
  });

  it("says when the phone did not measure", () => {
    expect(formatLength(null)).toBe("not measured");
  });
});

describe("osmLink", () => {
  it("points at the capture's location", () => {
    expect(osmLink(32.8801, -117.234)).toBe(
      "https://www.openstreetmap.org/?mlat=32.8801&mlon=-117.234#map=17/32.8801/-117.234",
    );
  });
});

describe("measurementOverlay", () => {
  const measurement = {
    snout: { x: 100, y: 200 },
    fork: { x: 900, y: 210 },
  };

  it("places snout and fork in the RGB frame's pixel space (the mask's)", () => {
    expect(
      measurementOverlay({ mask: { width: 1920, height: 1440 } }, measurement),
    ).toEqual({
      width: 1920,
      height: 1440,
      snout: { x: 100, y: 200 },
      fork: { x: 900, y: 210 },
    });
  });

  it("is null without a measurement or a known frame size", () => {
    expect(measurementOverlay({ mask: null }, measurement)).toBeNull();
    expect(
      measurementOverlay({ mask: { width: 1920, height: 1440 } }, null),
    ).toBeNull();
  });
});
