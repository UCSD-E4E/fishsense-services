// New in v2; covers fishsense-lite@77e8f8e5
// apps/fishsense-lite-web/app/portal/triage/actions.ts, which v1 left
// untested. Triage writes to Label Studio, not the v2 API, so the web's gate
// is the only one: it must run before anything is written -- and, since every
// tenant's projects share one Label Studio workspace, so must the check that
// the task is this tenant's.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { auth, acceptPrediction, deleteAnnotation, getTask, getAnnotation, getTenantProjectIds } =
  vi.hoisted(() => ({
    auth: vi.fn(),
    acceptPrediction: vi.fn(),
    deleteAnnotation: vi.fn(),
    getTask: vi.fn(),
    getAnnotation: vi.fn(),
    getTenantProjectIds: vi.fn(),
  }));

vi.mock("@/auth", () => ({ auth }));
vi.mock("@/lib/label-studio-tasks", () => ({
  acceptPrediction,
  deleteAnnotation,
  getTask,
  getAnnotation,
}));
vi.mock("@/lib/fishsense-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/fishsense-api")>()),
  getTenantProjectIds,
}));

import { acceptAction, undoAcceptAction } from "./actions";

const ADMIN = { user: { name: "A" }, role: "admin", isAdmin: true };
const RESULT = [{ from_name: "laser", type: "keypointlabels", value: { x: 1, y: 2 } }];
/** This tenant's laser project; 99 is another tenant's, in the same workspace. */
const OURS = 9;
const THEIRS = 99;

beforeEach(() => {
  auth.mockResolvedValue(ADMIN);
  acceptPrediction.mockResolvedValue(77);
  deleteAnnotation.mockResolvedValue(undefined);
  getTenantProjectIds.mockImplementation(async (kind: string) => (kind === "laser" ? [OURS] : []));
  getTask.mockImplementation(async (id: number) => ({ id, project: OURS }));
  getAnnotation.mockImplementation(async (id: number) => ({ id, task: 41 }));
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("acceptAction", () => {
  it("posts the prediction's regions verbatim, with the lead time", async () => {
    expect(await acceptAction(41, RESULT, 1234)).toEqual({ ok: true, annotationId: 77 });
    expect(acceptPrediction).toHaveBeenCalledExactlyOnceWith(41, RESULT, 1234);
  });

  it.each([
    ["no session", null, "Not authenticated"],
    ["a member who is not an admin", { ...ADMIN, role: "member", isAdmin: false }, "Not authorized"],
    ["no membership", { ...ADMIN, role: null, isAdmin: false }, "Not authorized"],
  ])("refuses %s before writing anything", async (_case, session, error) => {
    auth.mockResolvedValue(session);

    expect(await acceptAction(41, RESULT, 1234)).toEqual({ ok: false, error });
    expect(acceptPrediction).not.toHaveBeenCalled();
  });

  // v2: every tenant's projects share one Label Studio workspace, and this
  // action writes with the portal's own Label Studio key -- so an admin of one
  // tenant could otherwise annotate any task in the workspace by its id.
  it("refuses a task in another tenant's project before writing anything", async () => {
    getTask.mockResolvedValue({ id: 41, project: THEIRS });

    expect(await acceptAction(41, RESULT, 1234)).toEqual({
      ok: false,
      error: "Not this tenant's task",
    });
    expect(getTask).toHaveBeenCalledExactlyOnceWith(41);
    expect(acceptPrediction).not.toHaveBeenCalled();
  });

  it("returns Label Studio's failure instead of throwing", async () => {
    acceptPrediction.mockRejectedValue(new Error("annotation POST failed: 429"));
    expect(await acceptAction(41, RESULT, 1)).toEqual({
      ok: false,
      error: "annotation POST failed: 429",
    });
  });
});

describe("undoAcceptAction", () => {
  it("deletes the annotation it made", async () => {
    expect(await undoAcceptAction(77)).toEqual({ ok: true });
    expect(deleteAnnotation).toHaveBeenCalledExactlyOnceWith(77);
  });

  it("refuses a member who is not an admin before deleting anything", async () => {
    auth.mockResolvedValue({ ...ADMIN, role: "member", isAdmin: false });

    expect(await undoAcceptAction(77)).toEqual({ ok: false, error: "Not authorized" });
    expect(deleteAnnotation).not.toHaveBeenCalled();
  });

  it("refuses an annotation on another tenant's task before deleting anything", async () => {
    getTask.mockResolvedValue({ id: 41, project: THEIRS });

    expect(await undoAcceptAction(77)).toEqual({ ok: false, error: "Not this tenant's task" });
    expect(getAnnotation).toHaveBeenCalledExactlyOnceWith(77);
    expect(getTask).toHaveBeenCalledExactlyOnceWith(41);
    expect(deleteAnnotation).not.toHaveBeenCalled();
  });
});
