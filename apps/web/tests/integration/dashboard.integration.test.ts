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
    // Both the <title> head element and the <h1> body header carry it.
    expect(body).toContain("E4E FishSense");
  });

  it("renders the static Results section with both Superset cards", () => {
    expect(body).toContain("Results");
    expect(body).toContain("Lengths");
    expect(body).toContain("Metrics");
    expect(body).toContain(
      "https://analytics.fishsense.e4e.ucsd.edu/superset/dashboard/fishsense-fish-measurements",
    );
    expect(body).toContain(
      "https://analytics.fishsense.e4e.ucsd.edu/superset/dashboard/fishsense-pipeline-status",
    );
  });

  it("renders the Administration section with the Temporal Workflows link", () => {
    expect(body).toContain("Administration");
    expect(body).toContain("Workflows");
    expect(body).toContain("https://workflows.krg.ucsd.edu/namespaces/fishsense");
  });

  it("collapses labeling sections when the local DB has no LS-attributed labels", () => {
    // The container under test runs with LABEL_STUDIO_ENABLED unset, so no
    // project is asked for and every labeling list is empty. The
    // section-builder collapses empty categories — verifies that the
    // production SSR pipeline (fetch → buildSections → render) honors
    // the same empty-collapse contract the unit tests pin down, and that the
    // public landing renders with no API and no Label Studio behind it.
    expect(body).not.toMatch(/>\s*Laser Labeling\s*</);
    expect(body).not.toMatch(/>\s*Species Labeling\s*</);
    expect(body).not.toMatch(/>\s*Head\/Tail Labeling\s*</);
    expect(body).not.toMatch(/>\s*Slate Labeling\s*</);
  });
});
