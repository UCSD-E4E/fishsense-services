// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/triage-queue.test.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./label-projects", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./label-projects")>()),
  liveProjectIds: vi.fn(),
}));
vi.mock("./label-studio", () => ({ getProject: vi.fn() }));
vi.mock("./label-studio-tasks", () => ({ getTask: vi.fn(), listTasks: vi.fn() }));

import { liveProjectIds } from "./label-projects";
import { getProject } from "./label-studio";
import { listTasks } from "./label-studio-tasks";
import { loadQueue, reasonKey } from "./triage-queue";

const idsMock = vi.mocked(liveProjectIds);
const projectMock = vi.mocked(getProject);
const listTasksMock = vi.mocked(listTasks);

/** A project fetch answering from a `{id: [taskCount, finishedTaskCount]}` map. */
function projectsWithCounts(counts: Record<number, [number, number]>) {
  projectMock.mockImplementation(async (id: number) => ({
    id,
    title: `dive #${id} - Laser Calibration Labeling`,
    isPublished: true,
    taskCount: counts[id]?.[0],
    finishedTaskCount: counts[id]?.[1],
  }));
}

beforeEach(() => {
  idsMock.mockReset();
  projectMock.mockReset();
  listTasksMock.mockReset();
  listTasksMock.mockResolvedValue({ tasks: [], total: 0, drained: true });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("reasonKey", () => {
  // Reasons carry the task id, so grouping on the raw string would produce one
  // bucket per task and the counts would all read 1 — which is what the flat
  // five-reason sample effectively did.
  it("strips the task id so identical reasons group", () => {
    expect(reasonKey("task 41822: is_labeled")).toBe("is_labeled");
    expect(reasonKey("task 9: no prediction (0 present)")).toBe(
      "no prediction (0 present)",
    );
  });

  it("groups two tasks refused for the same reason", () => {
    expect(reasonKey("task 1: is_labeled")).toBe(reasonKey("task 2: is_labeled"));
  });

  it("leaves a reason carrying no task prefix alone", () => {
    expect(reasonKey("unpublished in Label Studio")).toBe(
      "unpublished in Label Studio",
    );
  });
});

describe("loadQueue — projects Label Studio reports finished", () => {
  // The same narrowing the landing page applies. Triage would find nothing in
  // such a project anyway (every task refuses on `is_labeled`), so the point
  // is the request it no longer spends, and the budget it no longer burns.
  it("does not page the task list of a finished project", async () => {
    idsMock.mockResolvedValue([200, 100]);
    projectsWithCounts({ 200: [56, 56], 100: [47, 12] });

    const report = await loadQueue("laser");

    expect(listTasksMock.mock.calls.map(([id]) => id)).toEqual([100]);
    const finished = report.projects.find((p) => p.projectId === 200)!;
    expect(finished.error).toMatch(/finished in Label Studio/);
  });

  // The finding that made this worth doing: the walk budget is what bounds a
  // load, and a run of finished projects at the front of the list used to
  // consume all of it — so triage reported an empty queue while older
  // projects held work. Candidates are walked newest first, so the finished
  // ones here are the high ids.
  it("does not let finished projects consume the walk budget", async () => {
    const finished = Array.from({ length: 12 }, (_, i) => 200 + i);
    idsMock.mockResolvedValue([...finished, 100]);
    projectsWithCounts({
      ...Object.fromEntries(finished.map((id) => [id, [10, 10]])),
      100: [47, 12],
    });

    await loadQueue("laser");

    expect(listTasksMock.mock.calls.map(([id]) => id)).toEqual([100]);
  });

  // ...but each skip still costs one project fetch, so the scan has to stay
  // bounded on its own. Without this a long enough list of finished projects
  // would fan out over Label Studio on a single page load — the shape that
  // earned the 429 the shared limiter exists for.
  it("bounds how many projects it resolves while looking for work", async () => {
    const ids = Array.from({ length: 60 }, (_, i) => 200 + i);
    idsMock.mockResolvedValue(ids);
    projectsWithCounts(Object.fromEntries(ids.map((id) => [id, [10, 10]])));

    const report = await loadQueue("laser");

    expect(projectMock.mock.calls.length).toBeLessThanOrEqual(24);
    expect(report.notWalked).toBeGreaterThan(0);
  });

  // Fails open exactly as the landing page does: an instance that stops
  // returning counts must leave triage walking every project it used to.
  it("still walks a project whose counts are unknown", async () => {
    idsMock.mockResolvedValue([200]);
    projectMock.mockResolvedValue({
      id: 200,
      title: "dive #200 - Laser Calibration Labeling",
      isPublished: true,
    });

    await loadQueue("laser");

    expect(listTasksMock.mock.calls.map(([id]) => id)).toEqual([200]);
  });
});

describe("the queue asks for its own kind's projects", () => {
  /**
   * This was hardcoded to "laser". With a second queue that is not a cosmetic
   * bug: the head/tail tab would walk the LASER projects, judge their tasks
   * against head/tail's rules, and report an empty queue while head/tail work
   * sat untouched — or, worse, offer a laser task under a head/tail heading.
   *
   * The key doubles as the api's URL segment, so this also pins that the two
   * vocabularies are the same string.
   */
  it.each(["laser", "headtail"] as const)("requests %s", async (kind) => {
    idsMock.mockResolvedValue([]);

    await loadQueue(kind);

    expect(idsMock).toHaveBeenCalledWith(kind, expect.any(Number));
  });
});
