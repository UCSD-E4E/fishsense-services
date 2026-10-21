// Missions and deployments, as the web shows and edits them (PLAN.md §4.9,
// Phase 2b).
//
// Pinned here:
//  * every call is made as the signed-in user, in the tenant from the URL,
//    and every slug and number is validated before it goes into a path;
//  * form values (a port, a purpose, a number) are checked before any request:
//    server actions are public endpoints;
//  * a missing mission or deployment is `null`, not an error page;
//  * the API's refusal of a port the owner's rules fix comes back as its words;
//  * a port is shown with what said so, and an unknown one says it is unknown.
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/client";
import type { MissionRow } from "./mission-facts";
import {
  createDeployment,
  getDeployment,
  getDeployments,
  getMission,
  getTenantMissions,
  PORTS,
  PURPOSES,
  parseMissionNumber,
  parsePort,
  parsePurpose,
  cameraLabel,
  isPhoneMission,
  sourceFilter,
  portLabel,
  portUnknown,
  purposeLabel,
  setMissionDeployment,
  setMissionPort,
  setMissionPurpose,
} from "./mission-facts";

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

function stub(body: unknown, status = 200) {
  const fetchMock = vi.fn<FetchSig>(async () => jsonResponse(body, status));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("reading", () => {
  it("lists the tenant's missions as the user", async () => {
    const fetchMock = stub([]);

    await getTenantMissions("user-token", "lab");

    const [request] = fetchMock.mock.calls[0];
    expect(request.url).toBe("http://api.test/tenants/lab/missions");
    expect(request.headers.get("Authorization")).toBe("Bearer user-token");
  });

  it("reads one mission, and a missing one is null", async () => {
    const fetchMock = stub({ number: 480 });
    await getMission("t", "lab", 480);
    expect(fetchMock.mock.calls[0][0].url).toBe(
      "http://api.test/tenants/lab/missions/480",
    );

    stub({ detail: "mission 9 not found" }, 404);
    expect(await getMission("t", "lab", 9)).toBeNull();
  });

  it("lists deployments, and reads one with its missions", async () => {
    const fetchMock = stub([]);
    await getDeployments("t", "lab");
    expect(fetchMock.mock.calls[0][0].url).toBe(
      "http://api.test/tenants/lab/deployments",
    );

    stub({ detail: "deployment 9 not found" }, 404);
    expect(await getDeployment("t", "lab", 9)).toBeNull();
  });

  it("refuses a slug or number that could steer the request", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(getMission("t", "../admin", 1)).rejects.toThrow(/tenant/);
    await expect(getMission("t", "lab", Number("1/../x"))).rejects.toThrow(
      /mission/,
    );
    await expect(getDeployment("t", "lab", -1)).rejects.toThrow(/deployment/);
  });
});

describe("writing", () => {
  it("states a port with the operator's note", async () => {
    const fetchMock = stub({ number: 7, port: "flat" });

    await setMissionPort("t", "lab", 7, "flat", "pool test");

    const [request] = fetchMock.mock.calls[0];
    expect(request.method).toBe("PUT");
    expect(request.url).toBe("http://api.test/tenants/lab/missions/7/port");
    expect(await request.json()).toEqual({ port: "flat", note: "pool test" });
  });

  it("brings back the API's words when a rule fixes the port", async () => {
    stub(
      { detail: "mission 7's port is corrective_optic by the owner's rule" },
      409,
    );

    const refused = setMissionPort("t", "lab", 7, "flat", null);

    await expect(refused).rejects.toBeInstanceOf(ApiError);
    await expect(refused).rejects.toThrow(/owner's rule/);
  });

  it("states and clears a purpose", async () => {
    const fetchMock = stub({ number: 7 });

    await setMissionPurpose("t", "lab", 7, "laser_calibration");
    await setMissionPurpose("t", "lab", 7, null);

    expect(await fetchMock.mock.calls[0][0].json()).toEqual({ purpose: "laser_calibration" });
    expect(await fetchMock.mock.calls[1][0].json()).toEqual({ purpose: null });
  });

  it("moves a mission into a deployment, and out of one", async () => {
    const fetchMock = vi.fn<FetchSig>(async (request) =>
      request.method === "DELETE"
        ? new Response(null, { status: 204 })
        : jsonResponse({ number: 7 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await setMissionDeployment("t", "lab", 7, 3);
    await setMissionDeployment("t", "lab", 7, null);

    expect(fetchMock.mock.calls[0][0].url).toBe(
      "http://api.test/tenants/lab/missions/7/deployment/3",
    );
    expect(fetchMock.mock.calls[1][0].method).toBe("DELETE");
  });

  it("creates a deployment", async () => {
    const fetchMock = stub({ number: 1, name: "trip" }, 201);

    await createDeployment("t", "lab", {
      name: "trip",
      started_on: "2026-03-02",
      ended_on: null,
      notes: null,
    });

    expect(await fetchMock.mock.calls[0][0].json()).toEqual({
      name: "trip",
      started_on: "2026-03-02",
      ended_on: null,
      notes: null,
    });
  });
});

describe("form values", () => {
  it("accepts only the ports and purposes the API knows", () => {
    expect(parsePort("flat")).toBe("flat");
    expect(parsePort("unknown")).toBe("unknown");
    expect(() => parsePort("pinhole")).toThrow(/port/);
    expect(parsePurpose("known_length")).toBe("known_length");
    expect(parsePurpose("")).toBeNull();
    expect(() => parsePurpose("fishing")).toThrow(/purpose/);
    // 0048: a calibration board is not a purpose; nearly every dive has one.
    expect(() => parsePurpose("slate")).toThrow(/purpose/);
  });

  it("accepts only a whole, non-negative mission number", () => {
    expect(parseMissionNumber("480")).toBe(480);
    expect(() => parseMissionNumber("4.5")).toThrow();
    expect(() => parseMissionNumber("1/../x")).toThrow();
    expect(() => parseMissionNumber("")).toThrow();
  });
});

describe("labels", () => {
  it("shows a port with what said so", () => {
    expect(portLabel("corrective_optic", "device_suffix")).toBe(
      "Corrective optic (D-labelled device)",
    );
    expect(portLabel("corrective_optic", "backfill")).toBe(
      "Corrective optic (ingested before the port was recorded)",
    );
    expect(portLabel("flat", "operator")).toBe("Flat port (stated by an operator)");
    expect(portLabel("unknown", null)).toBe("Unknown");
  });

  it("names purposes for people", () => {
    expect(purposeLabel("known_length")).toBe("Known length");
    expect(purposeLabel("laser_calibration")).toBe("Laser calibration");
    expect(purposeLabel(null)).toBe("Not stated");
  });
});

describe("the lists", () => {
  // The API builds its enums from mission_facts, which the database's CHECKs
  // pin; these lists are the web's copies, so they must say the same.
  const spec = JSON.parse(readFileSync(new URL("../openapi.json", import.meta.url), "utf8"));
  const enumOf = (field: string): string[] =>
    spec.components.schemas.Mission.properties[field].anyOf?.find(
      (option: { enum?: string[] }) => option.enum,
    )?.enum ?? spec.components.schemas.Mission.properties[field].enum;

  it("has the API's purposes and ports", () => {
    expect([...PURPOSES].sort()).toEqual([...enumOf("purpose")].sort());
    expect([...PORTS].sort()).toEqual([...enumOf("port")].sort());
  });
});

describe("missions by source (phase 7b)", () => {
  it("reads the filter from the URL", () => {
    expect(sourceFilter("mobile")).toBe("mobile");
    expect(sourceFilter("lite")).toBe("lite");
    expect(sourceFilter("nonsense")).toBeUndefined();
    expect(sourceFilter(undefined)).toBeUndefined();
  });
});

describe("a phone's mission (0057)", () => {
  const phone = { source: "mobile", port: "unknown", device_name: null } as MissionRow;
  const lite = { source: "lite", port: "unknown", device_name: "FSL-03" } as MissionRow;

  it("is told apart from a Lite one", () => {
    expect(isPhoneMission(phone)).toBe(true);
    expect(isPhoneMission(lite)).toBe(false);
  });

  it("has no camera port to be unknown", () => {
    expect(portUnknown(phone)).toBe(false);
    expect(portUnknown(lite)).toBe(true);
    expect(portUnknown({ ...lite, port: "flat" })).toBe(false);
  });

  it("names its camera as the phone, a Lite one by its name", () => {
    expect(cameraLabel(phone)).toBe("Phone");
    expect(cameraLabel(lite)).toBe("FSL-03");
    expect(cameraLabel({ ...lite, device_name: null })).toBe("—");
  });
});
