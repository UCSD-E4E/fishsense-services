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
  describeOrigin,
  placeName,
  formatLength,
  formatLengthWithError,
  millimetres,
  getCapture,
  fishOfCapture,
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

describe("fishOfCapture", () => {
  it("is the page of the capture's fish, where an old capture link goes (phase 7b)", () => {
    expect(fishOfCapture("lab", { capture_id: "c1", fish_id: "f1" })).toBe("/lab/fish/c1/f1");
  });

  it("is none for a capture without a fish", () => {
    expect(fishOfCapture("lab", { capture_id: "c1", fish_id: null })).toBeNull();
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

describe("formatLengthWithError", () => {
  it("shows the calibration's error bar, 1 SD, in whole millimetres (the owner, 2026-10-26: always with its error)", () => {
    expect(formatLengthWithError(0.4237, 0.0089)).toBe("424 ± 9 mm");
  });

  it("never rounds an error bar to nothing", () => {
    expect(formatLengthWithError(0.0312, 0.0004)).toBe("31 ± 1 mm");
  });

  it("is the plain length where no error bar was measured", () => {
    expect(formatLengthWithError(0.4237, null)).toBe("424 mm");
    expect(formatLengthWithError(0.4237, undefined)).toBe("424 mm");
  });
});

describe("formatLength", () => {
  it("shows whole millimetres (the owner, 2026-10-21)", () => {
    expect(formatLength(0.4237)).toBe("424 mm");
  });

  it("rounds to the nearest millimetre", () => {
    expect(formatLength(0.3125)).toBe("313 mm");
    expect(formatLength(0.31249)).toBe("312 mm");
  });

  it("groups the thousands of a fish over a metre", () => {
    expect(formatLength(1.2346)).toBe("1,235 mm");
  });

  it("says when the phone did not measure", () => {
    expect(formatLength(null)).toBe("not measured");
  });
});

describe("millimetres", () => {
  it("is the number alone, for a display that sets its unit apart", () => {
    expect(millimetres(0.4237)).toBe("424");
  });

  it("is a dash when there is no length", () => {
    expect(millimetres(null)).toBe("—");
    expect(millimetres(undefined)).toBe("—");
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

describe("describeOrigin", () => {
  it("says who placed the points", () => {
    expect(describeOrigin("auto")).toBe("found by the phone");
    expect(describeOrigin("manual")).toBe("placed by the user");
  });

  it("is honest about a version from before the app recorded it", () => {
    // fishsense-mobile #68: v7 overwrote edits in place.
    expect(describeOrigin("unknown")).toBe("not recorded (before app v8)");
  });
});

describe("placeName", () => {
  it("prefers the name the app recorded", () => {
    expect(
      placeName({
        place_name: "La Jolla Cove",
        derived_place_name: "San Diego",
      }),
    ).toEqual({ name: "La Jolla Cove", derived: false });
  });

  it("falls back to the name the server derived from the coordinates", () => {
    expect(
      placeName({ place_name: null, derived_place_name: "Scripps Pier" }),
    ).toEqual({
      name: "Scripps Pier",
      derived: true,
    });
  });

  it("is null when neither has a name", () => {
    expect(
      placeName({ place_name: null, derived_place_name: null }),
    ).toBeNull();
  });
});
