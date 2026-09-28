// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/dives.test.ts.
//
// v2 changes: tenant-scoped routes through the generated client; the
// signed-in user's own bearer token (the API checks their admin role itself)
// instead of a shared Basic-auth account; dives are addressed by `number`; and
// the API's reason for a refusal reaches the page.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearCalibrationSource,
  getDives,
  setCalibrationSource,
} from "./dives";

beforeEach(() => {
  vi.stubEnv("FISHSENSE_API_URL", "http://api.test");
  vi.stubEnv("FISHSENSE_TENANT", "lab");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

const TOKEN = "user-token";

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    statusText: init.statusText ?? "OK",
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
  });
}

type NextFetchInit = RequestInit & { next?: { revalidate?: number } };
type FetchSig = (input: Request, init?: NextFetchInit) => Promise<Response>;

const DIVE = {
  number: 2,
  name: "fish dive",
  dived_at: "2025-01-01T00:00:00Z",
  priority: "low",
  slate_template_number: null,
  calibration_source_number: 1,
};

describe("getDives", () => {
  it("GETs the tenant's dives with the user's bearer and returns the parsed body", async () => {
    const dives = [
      { ...DIVE, number: 1, name: "slate dive", slate_template_number: 5,
        calibration_source_number: null },
      DIVE,
    ]; // prettier-ignore
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse(dives));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getDives(TOKEN, 30);

    expect(result).toEqual(dives);
    const [request, init] = fetchMock.mock.calls[0];
    expect(request.url).toBe("http://api.test/tenants/lab/dives");
    expect(request.headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
    expect(init?.next?.revalidate).toBe(30);
  });

  it("throws on a non-OK response", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      new Response("nope", { status: 500, statusText: "Server Error" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getDives(TOKEN)).rejects.toThrow(/dives list failed: 500/);
  });
});

describe("setCalibrationSource", () => {
  it("PUTs to the path-param endpoint by dive number, as the user", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse(DIVE));
    vi.stubGlobal("fetch", fetchMock);

    await setCalibrationSource(TOKEN, 2, 1);

    const [request] = fetchMock.mock.calls[0];
    expect(request.url).toBe("http://api.test/tenants/lab/dives/2/calibration-source/1");
    expect(request.method).toBe("PUT");
    expect(request.headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
  });

  it("throws on a non-OK response", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      new Response("bad", { status: 400, statusText: "Bad Request" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(setCalibrationSource(TOKEN, 1, 1)).rejects.toThrow(
      /set calibration source failed: 400/,
    );
  });

  it("says why the API refused", async () => {
    // v2: the API enforces the admin role and the self-link rule itself, and
    // its reason is what the row should show.
    const fetchMock = vi.fn<FetchSig>(async () =>
      jsonResponse(
        { detail: "this needs the tenant's admin role" },
        { status: 403, statusText: "Forbidden" },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(setCalibrationSource(TOKEN, 2, 1)).rejects.toThrow(
      /403.*admin role/,
    );
  });

  it("rejects non-integer ids before making a request (SSRF guard)", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse(DIVE));
    vi.stubGlobal("fetch", fetchMock);

    await expect(setCalibrationSource(TOKEN, 1.5, 2)).rejects.toThrow(/Invalid diveNumber/);
    await expect(setCalibrationSource(TOKEN, 1, -3)).rejects.toThrow(/Invalid sourceNumber/);
    await expect(setCalibrationSource(TOKEN, Number.NaN, 2)).rejects.toThrow(
      /Invalid diveNumber/,
    );
    await expect(
      setCalibrationSource(TOKEN, "1/../../x" as unknown as number, 2),
    ).rejects.toThrow(/Invalid diveNumber/);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("clearCalibrationSource", () => {
  it("DELETEs the calibration-source endpoint as the user", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await clearCalibrationSource(TOKEN, 2);

    const [request] = fetchMock.mock.calls[0];
    expect(request.url).toBe("http://api.test/tenants/lab/dives/2/calibration-source");
    expect(request.method).toBe("DELETE");
    expect(request.headers.get("Authorization")).toBe(`Bearer ${TOKEN}`);
  });

  it("throws on a non-OK response", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      new Response("nope", { status: 404, statusText: "Not Found" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(clearCalibrationSource(TOKEN, 9)).rejects.toThrow(
      /clear calibration source failed: 404/,
    );
  });

  it("rejects a non-integer id before making a request", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(clearCalibrationSource(TOKEN, -1)).rejects.toThrow(/Invalid diveNumber/);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
