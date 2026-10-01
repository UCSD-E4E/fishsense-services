// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/active-projects.test.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./fishsense-api", () => ({
  getProjectIds: vi.fn(),
}));
vi.mock("./label-studio", () => ({
  getProjects: vi.fn(),
}));

import { getActiveProjects } from "./active-projects";
import { getProjectIds } from "./fishsense-api";
import { getProjects } from "./label-studio";

const idsMock = vi.mocked(getProjectIds);
const projectsMock = vi.mocked(getProjects);

beforeEach(() => {
  idsMock.mockReset();
  projectsMock.mockReset();
  // The Label Studio path is off by default (see labelStudioEnabled).
  // These tests cover the enabled path, so opt in explicitly.
  vi.stubEnv("LABEL_STUDIO_ENABLED", "true");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("getActiveProjects", () => {
  it("resolves IDs once, then resolves names per kind, returning a four-bucket map", async () => {
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [42, 43], "species": [70], "headtail": [44], "dive-slate": [66] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockImplementation(async (ids) => ({
      projects: ids.map((id) => ({ id, title: `p-${id}`, isPublished: true })),
      degraded: 0,
    }));

    const result = await getActiveProjects(60);

    expect(idsMock).toHaveBeenCalledTimes(4);
    expect(idsMock.mock.calls.map(([kind]) => kind).sort()).toEqual([
      "dive-slate",
      "headtail",
      "laser",
      "species",
    ]);
    expect(idsMock.mock.calls.every(([, revalidate]) => revalidate === 60)).toBe(true);
    expect(projectsMock).toHaveBeenCalledTimes(4);
    expect(projectsMock).toHaveBeenNthCalledWith(1, [42, 43], 60);
    expect(projectsMock).toHaveBeenNthCalledWith(2, [70], 60);
    expect(projectsMock).toHaveBeenNthCalledWith(3, [44], 60);
    expect(projectsMock).toHaveBeenNthCalledWith(4, [66], 60);

    expect(result).toEqual({
      laser: [
        { id: 42, title: "p-42", isPublished: true },
        { id: 43, title: "p-43", isPublished: true },
      ],
      species: [{ id: 70, title: "p-70", isPublished: true }],
      headtail: [{ id: 44, title: "p-44", isPublished: true }],
      slate: [{ id: 66, title: "p-66", isPublished: true }],
      degraded: 0,
      unavailable: [],
    });
  });

  it("sums degraded counts across kinds so the page can say the list is short", async () => {
    // Prod 2026-09-07: hosted LS 429'd 45 head/tail and 42 species project
    // lookups on one render. Both sections rendered short -- head/tail
    // vanished entirely -- with nothing on the page admitting it.
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [1], "species": [2], "headtail": [3], "dive-slate": [] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockImplementation(async (ids) => ({
      projects: ids.map((id) => ({ id, title: `p-${id}`, isPublished: true })),
      // One unreachable id in each of species and headtail; laser is clean.
      degraded: ids[0] === 1 ? 0 : 1,
    }));

    const result = await getActiveProjects(60);

    expect(result.degraded).toBe(2);
  });

  it("defaults revalidate to 300 seconds", async () => {
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [], "species": [], "headtail": [], "dive-slate": [] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockResolvedValue({ projects: [], degraded: 0 });

    await getActiveProjects();

    expect(idsMock).toHaveBeenCalledTimes(4);
    expect(idsMock.mock.calls.every(([, revalidate]) => revalidate === 300)).toBe(true);
    for (const call of projectsMock.mock.calls) {
      expect(call[1]).toBe(300);
    }
  });

  it("returns empty arrays for kinds with no incomplete projects", async () => {
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [], "species": [], "headtail": [], "dive-slate": [] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockResolvedValue({ projects: [], degraded: 0 });

    const result = await getActiveProjects(60);

    expect(result).toEqual({ laser: [], species: [], headtail: [], slate: [], degraded: 0, unavailable: [] });
  });
});

// Kill-switch behavior. With LABEL_STUDIO_ENABLED off (the default), the
// landing page must render without touching Label Studio at all: a single
// unresolvable project ID used to throw out of SSR and 500 the whole page.
// buildSections() drops any kind with zero projects, so empty buckets
// collapse the four labeling sections and leave Results + Administration.
describe("getActiveProjects (Label Studio disabled)", () => {
  beforeEach(() => {
    vi.stubEnv("LABEL_STUDIO_ENABLED", "false");
  });

  it("returns an empty four-bucket map", async () => {
    const result = await getActiveProjects(60);

    expect(result).toEqual({ laser: [], species: [], headtail: [], slate: [], degraded: 0, unavailable: [] });
  });

  it("does not call Label Studio", async () => {
    await getActiveProjects(60);

    expect(projectsMock).not.toHaveBeenCalled();
  });

  it("does not even fetch project IDs from fishsense-api", async () => {
    await getActiveProjects(60);

    expect(idsMock).not.toHaveBeenCalled();
  });

  it("resolves rather than throwing when Label Studio would 401", async () => {
    // Guards the actual prod failure: getProject threw on a 401 and the
    // rejection propagated through Promise.all out of the server component.
    projectsMock.mockRejectedValue(new Error("401 Unauthorized"));

    await expect(getActiveProjects(60)).resolves.toEqual({
      laser: [],
      species: [],
      headtail: [],
      slate: [],
      degraded: 0,
      unavailable: [],
    });
  });

  it("is disabled when LABEL_STUDIO_ENABLED is unset entirely", async () => {
    vi.unstubAllEnvs();

    await getActiveProjects(60);

    expect(idsMock).not.toHaveBeenCalled();
    expect(projectsMock).not.toHaveBeenCalled();
  });
});

describe("getActiveProjects — publish filtering", () => {
  it("hides unpublished projects from the landing page", async () => {
    // The real case: laser project 274728 was unpublished in Label Studio to
    // hold it back from labelers, but the id list comes from fishsense-api
    // (derived from label rows) and knows nothing about publish state — so
    // without this filter the held project stays linked from the landing page.
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [274728, 73], "species": [], "headtail": [], "dive-slate": [] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockImplementation(async (ids) => ({
      projects: ids.map((id) => ({
        id,
        title: `p-${id}`,
        isPublished: id !== 274728,
      })),
      degraded: 0,
    }));

    const result = await getActiveProjects(60);

    expect(result.laser).toEqual([{ id: 73, title: "p-73", isPublished: true }]);
  });

  it("filters every kind, not just laser", async () => {
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [1], "species": [2], "headtail": [3], "dive-slate": [4] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockImplementation(async (ids) => ({
      projects: ids.map((id) => ({ id, title: `p-${id}`, isPublished: false })),
      degraded: 0,
    }));

    const result = await getActiveProjects(60);

    expect(result).toEqual({ laser: [], species: [], headtail: [], slate: [], degraded: 0, unavailable: [] });
  });

  it("keeps drafts out while a project is still being populated", async () => {
    // Per-dive projects are created as drafts and only published once their
    // task set is complete, so a half-filled project must not appear either.
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [], "species": [100, 101], "headtail": [], "dive-slate": [] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockImplementation(async (ids) => ({
      projects: ids.map((id) => ({ id, title: `p-${id}`, isPublished: id === 100 })),
      degraded: 0,
    }));

    const result = await getActiveProjects(60);

    expect(result.species.map((p) => p.id)).toEqual([100]);
  });
});

describe("getActiveProjects — completion filtering", () => {
  // Prod, 2026-09-08: dive 516's species project (285759) had its last task
  // annotated at 05:47 UTC and stayed on the landing page until the hourly
  // sync ran just after 06:00. fishsense-api derives "outstanding" from our
  // own `SpeciesLabel.completed`, which only that sync writes — so the card
  // list lagged Label Studio by up to a full sync cycle. Label Studio's own
  // counts arrive on the same project fetch that resolves the title, so the
  // page can just ask.
  it("hides a project Label Studio says is fully labeled", async () => {
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [], "species": [285759, 285676], "headtail": [], "dive-slate": [] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockImplementation(async (ids) => ({
      projects: ids.map((id) => ({
        id,
        title: `p-${id}`,
        isPublished: true,
        taskCount: id === 285759 ? 56 : 47,
        finishedTaskCount: id === 285759 ? 56 : 12,
      })),
      degraded: 0,
    }));

    const result = await getActiveProjects(60);

    expect(result.species.map((p) => p.id)).toEqual([285676]);
  });

  it("filters every kind, not just species", async () => {
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [1], "species": [2], "headtail": [3], "dive-slate": [4] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockImplementation(async (ids) => ({
      projects: ids.map((id) => ({
        id,
        title: `p-${id}`,
        isPublished: true,
        taskCount: 10,
        finishedTaskCount: 10,
      })),
      degraded: 0,
    }));

    const result = await getActiveProjects(60);

    expect(result).toEqual({ laser: [], species: [], headtail: [], slate: [], degraded: 0, unavailable: [] });
  });

  // Fails open: a Label Studio that stops returning counts must not blank the
  // page. The api-derived list stays the floor.
  it("keeps every project when Label Studio reports no counts", async () => {
    idsMock.mockImplementation(async (kind) =>
      ({ "laser": [], "species": [1, 2], "headtail": [], "dive-slate": [] } as Record<string, number[]>)[kind] ?? [],
    );
    projectsMock.mockImplementation(async (ids) => ({
      projects: ids.map((id) => ({ id, title: `p-${id}`, isPublished: true })),
      degraded: 0,
    }));

    const result = await getActiveProjects(60);

    expect(result.species.map((p) => p.id)).toEqual([1, 2]);
  });
});

// v2: the web asks the API with its own Authentik service token, so an
// Authentik or API outage fails the ID fetch itself. The public landing page
// must stay up (its Results and Administration links need neither), say what
// it could not list, and never present an empty labeling list as the answer.
describe("getActiveProjects — when the API cannot be asked", () => {
  it("drops only the kind it could not ask about, and says so", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    idsMock.mockImplementation(async (kind) => {
      if (kind === "species") throw new Error("fishsense-api species project IDs failed");
      return ({ "laser": [42], "headtail": [], "dive-slate": [] } as Record<string, number[]>)[kind] ?? [];
    });
    projectsMock.mockImplementation(async (ids) => ({
      projects: ids.map((id) => ({ id, title: `p-${id}`, isPublished: true })),
      degraded: 0,
    }));

    const result = await getActiveProjects(60);

    expect(result.laser).toEqual([{ id: 42, title: "p-42", isPublished: true }]);
    expect(result.species).toEqual([]);
    expect(result.unavailable).toEqual(["species"]);
    expect(projectsMock).toHaveBeenCalledTimes(1);
  });

  it("renders no labeling work, rather than throwing, when Authentik is down", async () => {
    // Every ID fetch starts by minting the web's service token; with the
    // issuer unreachable every one of them throws.
    vi.spyOn(console, "error").mockImplementation(() => {});
    idsMock.mockRejectedValue(new TypeError("fetch failed"));

    const result = await getActiveProjects(60);

    expect(result).toEqual({
      laser: [],
      species: [],
      headtail: [],
      slate: [],
      degraded: 0,
      unavailable: ["laser", "species", "headtail", "dive-slate"],
    });
    expect(projectsMock).not.toHaveBeenCalled();
  });

  it("logs the failure it swallowed, so an outage is not silent", async () => {
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    idsMock.mockRejectedValue(new TypeError("fetch failed"));

    await getActiveProjects(60);

    expect(logged).toHaveBeenCalled();
  });
});
