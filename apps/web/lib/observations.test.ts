// Every measured fish, Lite and Mobile alike, as the web shows it
// (PLAN.md §4.9, Phase 1 of §9.26).
//
// Pinned here:
//  * every call is made as the signed-in user, in a tenant they belong to (the
//    API enforces it; the web only ever asks for the caller's own tenants);
//  * a tenant slug, capture id and fish id from the URL are validated before
//    they go into an API path (request forgery), and so is the device filter;
//  * a missing fish is `null`, not an error page;
//  * a fish is named by its species or its fish model, and a model's
//    measurement is shown against its known length;
//  * a mobile fish's page is its capture's (/captures), a Lite fish's is /fish.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  deviceFilter,
  fishName,
  formatError,
  getObservation,
  getObservationPage,
  knownLengthError,
  methodLabel,
  observationHref,
  where,
  type Observation,
} from "./observations";
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

const CAPTURE = "6f1c8a52-9a4e-4c43-9b8e-4f4e2d1a0b11";
const FISH = "0b6a7f3e-2c1d-4e5f-8a9b-1c2d3e4f5a6b";

function observation(overrides: Partial<Observation> = {}): Observation {
  return {
    capture_id: CAPTURE,
    fish_id: FISH,
    method: "laser",
    length_m: 0.305,
    captured_at: "2025-01-18T01:00:00Z",
    device: { kind: "lite", name: "FSL-10" },
    dive: { number: 2, name: "FSL-10D Ginny" },
    species: null,
    fish_model: { name: "Ginny", known_length_m: 0.3127 },
    location: null,
    place_name: null,
    client_capture_uuid: null,
    photo_url: null,
    ...overrides,
  };
}

describe("getObservationPage", () => {
  it("lists a tenant's fish as the signed-in user", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      jsonResponse({ observations: [], next: null }),
    );
    vi.stubGlobal("fetch", fetchMock);

    expect(await getObservationPage("user-token", "lab")).toEqual({
      observations: [],
      next: null,
    });

    const [request, init] = fetchMock.mock.calls[0];
    expect(request.url).toBe("http://api.test/tenants/lab/observations");
    expect(request.headers.get("Authorization")).toBe("Bearer user-token");
    expect(request.cache === "no-store" || init?.cache === "no-store").toBe(
      true,
    );
  });

  it("passes the device filter and the page cursor through", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      jsonResponse({ observations: [], next: null }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getObservationPage("t", "lab", { device: "lite", before: "c~1~2" });

    const url = new URL(fetchMock.mock.calls[0][0].url);
    expect(url.searchParams.get("device")).toBe("lite");
    expect(url.searchParams.get("before")).toBe("c~1~2");
  });

  it("refuses a tenant slug that could steer the request", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(getObservationPage("t", "../admin")).rejects.toThrow(
      /tenant/,
    );
  });

  it("throws the API's refusal", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchSig>(async () => jsonResponse({ detail: "no" }, 500)),
    );
    await expect(getObservationPage("t", "lab")).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});

describe("getObservation", () => {
  it("asks for one fish in one capture", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      jsonResponse({ observation: observation() }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getObservation("t", "lab", CAPTURE, FISH);

    expect(fetchMock.mock.calls[0][0].url).toBe(
      `http://api.test/tenants/lab/observations/${CAPTURE}/${FISH}`,
    );
  });

  it("is null when the API has no such fish", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchSig>(async () => jsonResponse({ detail: "Not Found" }, 404)),
    );
    expect(await getObservation("t", "lab", CAPTURE, FISH)).toBeNull();
  });

  it("refuses ids that are not uuids", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(getObservation("t", "lab", "../x", FISH)).rejects.toThrow(
      /capture/,
    );
    await expect(getObservation("t", "lab", CAPTURE, "1 OR 1")).rejects.toThrow(
      /fish/,
    );
  });
});

describe("deviceFilter", () => {
  it("keeps only the filters the API knows", () => {
    expect(deviceFilter("lite")).toBe("lite");
    expect(deviceFilter("mobile")).toBe("mobile");
    expect(deviceFilter("other")).toBe("other");
    expect(deviceFilter(undefined)).toBeUndefined();
    expect(deviceFilter("../../admin")).toBeUndefined();
  });
});

describe("fishName", () => {
  it("names a species by its common and scientific names", () => {
    const o = observation({
      fish_model: null,
      species: { scientific_name: "Lachnolaimus maximus", common_name: "Hogfish" },
    });
    expect(fishName(o)).toEqual({
      name: "Hogfish",
      detail: "Lachnolaimus maximus",
    });
  });

  it("names a species by whichever name it has", () => {
    expect(
      fishName(
        observation({
          fish_model: null,
          species: { scientific_name: null, common_name: "Garibaldi" },
        }),
      ),
    ).toEqual({ name: "Garibaldi", detail: null });
    expect(
      fishName(
        observation({
          fish_model: null,
          species: { scientific_name: "Hypsypops rubicundus", common_name: null },
        }),
      ),
    ).toEqual({ name: "Hypsypops rubicundus", detail: null });
  });

  it("names a fish model as one", () => {
    expect(fishName(observation())).toEqual({
      name: "Ginny",
      detail: "fish model",
    });
  });

  it("says so when there is neither", () => {
    expect(fishName(observation({ fish_model: null }))).toEqual({
      name: "Unidentified fish",
      detail: null,
    });
  });
});

describe("knownLengthError", () => {
  it("is the measurement's error against a model's known length", () => {
    expect(knownLengthError(observation())).toBeCloseTo(-0.0246, 3);
  });

  it("is null without a model, a known length, or a length", () => {
    expect(knownLengthError(observation({ fish_model: null }))).toBeNull();
    expect(
      knownLengthError(
        observation({ fish_model: { name: "X", known_length_m: null } }),
      ),
    ).toBeNull();
    expect(knownLengthError(observation({ length_m: null }))).toBeNull();
  });
});

describe("formatError", () => {
  it("signs an error and gives it to a tenth of a percent", () => {
    expect(formatError(0.0172)).toBe("+1.7%");
    expect(formatError(-0.0246)).toBe("-2.5%");
    expect(formatError(0)).toBe("0.0%");
  });
});

describe("methodLabel", () => {
  it("says how the depth was found", () => {
    expect(methodLabel("laser")).toBe("Laser");
    expect(methodLabel("lidar")).toBe("LiDAR");
    expect(methodLabel("something-new")).toBe("something-new");
  });
});

describe("where", () => {
  it("is the dive for a Lite fish and the place for a mobile one", () => {
    expect(where(observation())).toBe("FSL-10D Ginny");
    expect(
      where(
        observation({
          method: "lidar",
          dive: null,
          place_name: "Scripps Pier",
        }),
      ),
    ).toBe("Scripps Pier");
    expect(where(observation({ dive: null }))).toBeNull();
  });
});

describe("observationHref", () => {
  it("sends a mobile fish to its capture's page", () => {
    const o = observation({
      method: "lidar",
      client_capture_uuid: "7ceaecf8-2eab-5bb9-a429-c4b68ae38e76",
    });
    expect(observationHref("lab", o)).toBe(
      "/captures/lab/7ceaecf8-2eab-5bb9-a429-c4b68ae38e76",
    );
  });

  it("sends a Lite fish to its own page", () => {
    expect(observationHref("lab", observation())).toBe(
      `/fish/lab/${CAPTURE}/${FISH}`,
    );
  });
});
