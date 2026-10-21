// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/tests/integration/dashboard.integration.test.ts.
import { beforeAll, describe, expect, it } from "vitest";

const WEB_URL = process.env.FISHSENSE_WEB_URL ?? "http://localhost:3000";

let body: string;
let status: number;

beforeAll(async () => {
  // The container can take a moment to boot after `docker compose up
  // -d` returns — retry the homepage until it responds 2xx, then snapshot
  // the body for the remaining tests so each one is a pure assertion on
  // the same SSR'd HTML (one network round-trip, not six).
  let lastError: unknown;
  for (let i = 0; i < 30; i += 1) {
    try {
      const res = await fetch(`${WEB_URL}/`, { cache: "no-store" });
      if (res.ok) {
        body = await res.text();
        status = res.status;
        return;
      }
      lastError = new Error(`status=${res.status}`);
    } catch (e) {
      lastError = e;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error(
    `fishsense-web never responded 2xx at ${WEB_URL}: ${String(lastError)}`,
  );
});

describe("fishsense-web SSR (against the running container)", () => {
  it("returns HTTP 200 from /", () => {
    expect(status).toBe(200);
  });

  it("renders the dashboard title", () => {
    // The <title> head element; the <h1> and the shell's mark say it too.
    expect(body).toContain("<title>FishSense</title>");
  });

  it("is the public page: what FishSense does, and no dashboards", () => {
    expect(body).toContain("A fish&#x27;s length, from a photo.");
    expect(body).toContain("Two ways to measure");
    // Superset and Temporal are staff's (Today), never the public home's.
    expect(body).not.toContain("analytics.fishsense.e4e.ucsd.edu");
    expect(body).not.toContain("workflows.krg.ucsd.edu");
  });
});
