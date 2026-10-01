// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/portal-sections.test.ts.
import { describe, expect, it } from "vitest";
import { PORTAL_SECTIONS } from "./portal-sections";

/**
 * The portal index is a hub: it does nothing but route to the pages that do
 * the work. Listing those pages in one place is what keeps the hub honest —
 * a section added to the array appears on the index, and one that is only
 * ever a route nobody can reach from the index is a page users cannot find.
 */
describe("PORTAL_SECTIONS", () => {
  it("lists something", () => {
    expect(PORTAL_SECTIONS.length).toBeGreaterThan(0);
  });

  it("covers the two pages the portal owns", () => {
    expect(PORTAL_SECTIONS.map((s) => s.href).sort()).toEqual([
      "/portal/calibration",
      "/portal/triage",
    ]);
  });

  // Every href is gated by the same authorization as the index itself.
  // An outside link would take a labeler off the portal without warning.
  it("only links within the portal", () => {
    for (const section of PORTAL_SECTIONS) {
      expect(section.href.startsWith("/portal/")).toBe(true);
    }
  });

  it("gives each section a distinct href and title", () => {
    expect(new Set(PORTAL_SECTIONS.map((s) => s.href)).size).toBe(
      PORTAL_SECTIONS.length,
    );
    expect(new Set(PORTAL_SECTIONS.map((s) => s.title)).size).toBe(
      PORTAL_SECTIONS.length,
    );
  });

  it("says what each page is for, not just what it is called", () => {
    for (const section of PORTAL_SECTIONS) {
      expect(section.title.length).toBeGreaterThan(0);
      // A card with no description makes the hub a bare list of nouns, which
      // is exactly what a labeler landing here cannot act on.
      expect(section.description.length).toBeGreaterThan(20);
    }
  });
});
