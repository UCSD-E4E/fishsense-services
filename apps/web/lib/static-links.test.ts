import { describe, expect, it } from "vitest";
import * as links from "./static-links";

describe("static links", () => {
  // Superset was retired on 2026-10-23: its dashboards' links bounced staff
  // to a sign-in nobody had used since 2026-10-02.
  it("points nowhere at the retired Superset", () => {
    const all = Object.values(links).flat() as { href?: string }[];
    expect(all.length).toBeGreaterThan(0);
    for (const link of all) {
      expect(link.href ?? "").not.toContain("analytics.fishsense.e4e.ucsd.edu");
    }
    expect(links).not.toHaveProperty("RESULTS_LINKS");
  });
});
