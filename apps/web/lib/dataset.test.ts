import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getDatasetRegions } from "./dataset";

const KEYS = { name: "Florida Keys, USA", latitude: 27, longitude: -81.5, fish: 4 };

function answering(body: unknown, status = 200) {
  const fetchMock = vi.fn(
    async (_request: Request) =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("getDatasetRegions", () => {
  beforeEach(() => vi.stubEnv("FISHSENSE_API_URL", "http://api.test"));
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("asks the API for every tenant's map, as the signed-in user", async () => {
    const fetchMock = answering({ regions: [KEYS], elsewhere: 2 });

    const found = await getDatasetRegions("t");

    expect(fetchMock.mock.calls[0][0].url).toBe("http://api.test/admin/regions");
    expect(fetchMock.mock.calls[0][0].headers.get("authorization")).toBe("Bearer t");
    expect(found).toEqual({ regions: [KEYS], elsewhere: 2 });
  });

  it("asks for the automatic lengths' map when told to", async () => {
    const fetchMock = answering({ regions: [], elsewhere: 0 });

    await getDatasetRegions("t", "automatic");

    expect(fetchMock.mock.calls[0][0].url).toBe("http://api.test/admin/regions?source=automatic");
  });

  it("is nothing for someone who is not a system admin (the API's 404)", async () => {
    answering({ detail: "Not Found" }, 404);

    expect(await getDatasetRegions("t")).toBeNull();
  });
});
