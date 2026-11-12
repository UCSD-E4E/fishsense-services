import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DatasetMap } from "./dataset-map";

type FetchSig = (input: Request, init?: RequestInit) => Promise<Response>;

const REGIONS = {
  regions: [
    { name: "Florida Keys, USA", latitude: 27.0, longitude: -81.5, fish: 2304 },
    { name: "Caribbean", latitude: 18.0, longitude: -75.0, fish: 7 },
  ],
  elsewhere: 12,
};

function api(status: number, body: unknown) {
  const fetchMock = vi.fn<FetchSig>(
    async () =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function html(section: Promise<ReactElement>): Promise<string> {
  return renderToStaticMarkup(await section);
}

beforeEach(() => vi.stubEnv("FISHSENSE_API_URL", "http://api.test"));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("DatasetMap", () => {
  it("puts every tenant's fish on the map by region", async () => {
    const fetchMock = api(200, REGIONS);

    const out = await html(DatasetMap({ token: "t", source: "human" }));

    expect(new URL(fetchMock.mock.calls[0][0].url).pathname).toBe("/admin/regions");
    expect(out).toContain("Florida Keys, USA");
    expect(out).toContain("2,304 fish");
    expect(out).toContain("12 more fish without a region");
    expect(out).not.toContain("not reviewed by a person");
  });

  it("maps the automatic lengths, and says nobody has reviewed them", async () => {
    const fetchMock = api(200, REGIONS);

    const out = await html(DatasetMap({ token: "t", source: "automatic" }));

    expect(new URL(fetchMock.mock.calls[0][0].url).searchParams.get("source")).toBe("automatic");
    expect(out).toContain("not reviewed by a person");
    expect(out).toContain("12 more automatic lengths without a region");
  });

  it("says it could not load the map", async () => {
    api(500, "down");

    const out = await html(DatasetMap({ token: "t", source: "human" }));

    expect(out).toContain('role="alert"');
    expect(out).toContain("Could not load the map");
  });

  it("says it is for system admins when the API says the caller is not one", async () => {
    api(404, { detail: "Not Found" });

    const out = await html(DatasetMap({ token: "t", source: "human" }));

    expect(out).toContain("only for system admins");
    expect(out).not.toContain("world.svg");
  });
});
