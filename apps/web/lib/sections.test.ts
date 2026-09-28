// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/sections.test.ts.
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProjectsByKind } from "./active-projects";
import { buildSections } from "./sections";
import type { StaticLink } from "./static-links";

afterEach(() => {
  vi.unstubAllEnvs();
});

const EMPTY_ACTIVE: ProjectsByKind = {
  laser: [],
  species: [],
  headtail: [],
  slate: [],
};

const NO_STATIC: { results: StaticLink[]; admin: StaticLink[] } = {
  results: [],
  admin: [],
};

describe("buildSections", () => {
  it("omits a labeling category when its project list is empty", () => {
    const sections = buildSections(EMPTY_ACTIVE, NO_STATIC);
    expect(sections.map((s) => s.title)).toEqual([]);
  });

  it("turns a single labeling kind into a Labeling-suffixed section", () => {
    const sections = buildSections(
      { ...EMPTY_ACTIVE, laser: [{ id: 42, title: "REEF Laser High" }] },
      NO_STATIC,
    );
    expect(sections).toHaveLength(1);
    expect(sections[0].title).toBe("Laser Labeling");
    expect(sections[0].links).toEqual([
      {
        title: "REEF Laser High",
        description: "REEF Laser High labeling project",
        // Default hosted-LS base when no env is set.
        href: "https://app.heartex.com/projects/42",
      },
    ]);
  });

  it("derives the project link base from LABEL_STUDIO_URL, overridable by LABELER_BASE", () => {
    vi.stubEnv("LABEL_STUDIO_URL", "https://ls.example.com");
    expect(
      buildSections(
        { ...EMPTY_ACTIVE, laser: [{ id: 42, title: "P" }] },
        NO_STATIC,
      )[0].links[0].href,
    ).toBe("https://ls.example.com/projects/42");

    vi.stubEnv("LABELER_BASE", "https://override.example.com");
    expect(
      buildSections(
        { ...EMPTY_ACTIVE, laser: [{ id: 42, title: "P" }] },
        NO_STATIC,
      )[0].links[0].href,
    ).toBe("https://override.example.com/projects/42");
  });

  // v2 change: compose passes LABELER_BASE through from the shell, empty when
  // unset. v1's `??` read "" as a base and linked every card to
  // `/projects/42` on the portal's own host.
  it("treats an empty LABELER_BASE as unset", () => {
    vi.stubEnv("LABEL_STUDIO_URL", "https://ls.example.com");
    vi.stubEnv("LABELER_BASE", "");
    expect(
      buildSections(
        { ...EMPTY_ACTIVE, laser: [{ id: 42, title: "P" }] },
        NO_STATIC,
      )[0].links[0].href,
    ).toBe("https://ls.example.com/projects/42");
  });

  it("emits sections in the canonical order: Laser, Head/Tail, Species, Slate, Results, Administration", () => {
    const sections = buildSections(
      {
        laser: [{ id: 1, title: "L" }],
        species: [{ id: 2, title: "S" }],
        headtail: [{ id: 3, title: "H" }],
        slate: [{ id: 4, title: "D" }],
      },
      {
        results: [{ title: "Lengths", description: "d", href: "https://r/" }],
        admin: [{ title: "Workflows", description: "w", href: "https://a/" }],
      },
    );
    expect(sections.map((s) => s.title)).toEqual([
      "Laser Labeling",
      "Head/Tail Labeling",
      "Species Labeling",
      "Slate Labeling",
      "Results",
      "Administration",
    ]);
  });

  it("uses Head/Tail (with slash) and Slate as the human labels", () => {
    const sections = buildSections(
      {
        ...EMPTY_ACTIVE,
        headtail: [{ id: 1, title: "H" }],
        slate: [{ id: 2, title: "D" }],
      },
      NO_STATIC,
    );
    expect(sections.map((s) => s.title)).toEqual(["Head/Tail Labeling", "Slate Labeling"]);
  });

  it("preserves project order within a kind", () => {
    const sections = buildSections(
      {
        ...EMPTY_ACTIVE,
        laser: [
          { id: 42, title: "first" },
          { id: 43, title: "second" },
          { id: 44, title: "third" },
        ],
      },
      NO_STATIC,
    );
    expect(sections[0].links.map((l) => l.title)).toEqual(["first", "second", "third"]);
  });

  it("omits Results and Administration when their static link arrays are empty", () => {
    const sections = buildSections(EMPTY_ACTIVE, { results: [], admin: [] });
    expect(sections).toEqual([]);
  });

  it("keeps Results / Administration even when no labeling projects exist", () => {
    const sections = buildSections(EMPTY_ACTIVE, {
      results: [{ title: "Lengths", description: "d", href: "https://r/" }],
      admin: [{ title: "Workflows", description: "w", href: "https://a/" }],
    });
    expect(sections.map((s) => s.title)).toEqual(["Results", "Administration"]);
  });
});
