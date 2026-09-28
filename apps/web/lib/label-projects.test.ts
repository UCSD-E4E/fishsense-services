// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/label-projects.test.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./fishsense-api", () => ({ getProjectIds: vi.fn() }));

import { getProjectIds } from "./fishsense-api";
import { hasOutstandingTasks, isPublished, liveProjectIds } from "./label-projects";

const idsMock = vi.mocked(getProjectIds);

beforeEach(() => {
  idsMock.mockReset();
  idsMock.mockResolvedValue([7, 3, 9]);
  vi.stubEnv("LABEL_STUDIO_ENABLED", "true");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("liveProjectIds", () => {
  it("asks the API for the kind's outstanding projects", async () => {
    expect(await liveProjectIds("laser", 60)).toEqual([7, 3, 9]);
    expect(idsMock).toHaveBeenCalledExactlyOnceWith("laser", 60);
  });

  // The kill switch is the check triage never had: with Label Studio off the
  // landing page went blank while triage carried on calling it. Sharing this
  // function is what makes that impossible to get wrong in one place only.
  it("returns nothing, and asks nothing, when Label Studio is switched off", async () => {
    vi.stubEnv("LABEL_STUDIO_ENABLED", "false");
    expect(await liveProjectIds("laser", 60)).toEqual([]);
    expect(idsMock).not.toHaveBeenCalled();
  });

  // Order belongs to the consumer: the landing page shows cards in API order,
  // triage wants newest dive first. Imposing one here would change the other.
  it("does not reorder", async () => {
    expect(await liveProjectIds("species", 60)).toEqual([7, 3, 9]);
  });

  // A discovery failure must reach the caller. Swallowing it into an empty
  // list reports a drained queue while Label Studio is full of work.
  it("propagates a failure rather than returning empty", async () => {
    idsMock.mockRejectedValue(new Error("503 Service Unavailable"));
    await expect(liveProjectIds("laser", 60)).rejects.toThrow(/503/);
  });
});

describe("isPublished", () => {
  it("keeps a published project", () => {
    expect(isPublished({ id: 1, title: "p", isPublished: true })).toBe(true);
  });

  it("drops an explicitly unpublished one", () => {
    expect(isPublished({ id: 1, title: "p", isPublished: false })).toBe(false);
  });

  // Fails open: only an explicit flag hides a project, so a Label Studio
  // response change cannot silently blank every surface at once.
  it("keeps one whose publish state is unknown", () => {
    expect(isPublished({ id: 1, title: "p" })).toBe(true);
  });
});

describe("hasOutstandingTasks", () => {
  const project = (taskCount?: number, finishedTaskCount?: number) => ({
    id: 1,
    title: "p",
    taskCount,
    finishedTaskCount,
  });

  it("keeps a project with unlabeled tasks left", () => {
    expect(hasOutstandingTasks(project(47, 12))).toBe(true);
  });

  // The case this whole filter exists for: fishsense-api still says the
  // project is outstanding, because our `completed` column only learns
  // otherwise at the next hourly Label Studio sync.
  it("drops one Label Studio says is fully labeled", () => {
    expect(hasOutstandingTasks(project(56, 56))).toBe(false);
  });

  // An incoherent pair is an unusable answer, not a strong one. Hiding on it
  // would bury real labeling work on the strength of a reply we already know
  // is wrong — the same reasoning that keeps a project whose counts are
  // missing or non-numeric.
  it("keeps one reporting more finished than it holds", () => {
    expect(hasOutstandingTasks(project(56, 57))).toBe(true);
  });

  // Fails open, like `isPublished`: an absent count means we did not ask
  // Label Studio, not that the work is done.
  it("keeps one whose counts are unknown", () => {
    expect(hasOutstandingTasks(project())).toBe(true);
    expect(hasOutstandingTasks(project(56, undefined))).toBe(true);
    expect(hasOutstandingTasks(project(undefined, 56))).toBe(true);
  });

  // Vacuous truth reads as "not complete" here, the same convention
  // `dive_pipeline_status`'s `*_labeling_complete` flags use: a project with
  // no tasks at all has not finished anything, and hiding it would bury a
  // project whose population failed.
  it("keeps an empty project", () => {
    expect(hasOutstandingTasks(project(0, 0))).toBe(true);
  });

  // A non-numeric count is unknown, not zero.
  it("keeps one whose counts are not numbers", () => {
    expect(
      hasOutstandingTasks({
        id: 1,
        title: "p",
        taskCount: Number.NaN,
        finishedTaskCount: 0,
      }),
    ).toBe(true);
  });
});
