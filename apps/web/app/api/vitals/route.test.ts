// The browsers' timings, passed to the API as the web's service account
// (migration 0091; the owner, 2026-11-12).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { getServiceToken } = vi.hoisted(() => ({ getServiceToken: vi.fn() }));
vi.mock("@/lib/service-token", () => ({ getServiceToken }));

import pkg from "../../../package.json";
import { POST } from "./route";

const TTFB = { route: "/[tenant]/fish", metric: "TTFB", value: 640.5, kind: "load" };

const post = (body: string) =>
  POST(new Request("http://0.0.0.0:3000/api/vitals", { method: "POST", body }));

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubEnv("FISHSENSE_API_URL", "http://api.test");
  getServiceToken.mockResolvedValue("service-token");
  fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.clearAllMocks();
});

async function sent(call = 0): Promise<{ url: string; auth: string | null; body: unknown }> {
  const request = fetchMock.mock.calls[call][0] as Request;
  return {
    url: request.url,
    auth: request.headers.get("authorization"),
    body: JSON.parse(await request.text()),
  };
}

describe("POST /api/vitals", () => {
  it("passes the timings, with the web's release, to the API", async () => {
    const response = await post(JSON.stringify([TTFB]));

    expect(response.status).toBe(204);
    const { url, auth, body } = await sent();
    expect(url).toBe("http://api.test/telemetry/page-timings");
    expect(auth).toBe("Bearer service-token");
    expect(body).toEqual({ release: pkg.version, timings: [TTFB] });
  });

  it("mints a new token once when the API refuses the cached one", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    const response = await post(JSON.stringify([TTFB]));

    expect(response.status).toBe(204);
    expect(getServiceToken).toHaveBeenLastCalledWith(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("refuses a body that holds no timing, and asks the API nothing", async () => {
    for (const body of ["not json", "{}", "[]", JSON.stringify([{ ...TTFB, metric: "X" }])]) {
      expect((await post(body)).status).toBe(400);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses a body over 16 KB unread", async () => {
    const response = await post("x".repeat(16 * 1024 + 1));

    expect(response.status).toBe(413);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("answers the browser the same when the API is down: a timing is not worth an error", async () => {
    fetchMock.mockRejectedValue(new Error("connect ECONNREFUSED"));
    vi.spyOn(console, "error").mockImplementation(() => {});

    expect((await post(JSON.stringify([TTFB]))).status).toBe(204);
  });
});
