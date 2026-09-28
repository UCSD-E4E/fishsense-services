// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/task-shape.test.ts.
import { describe, expect, it } from "vitest";
import listFixture from "./__fixtures__/task-list.json";
import detailFixture from "./__fixtures__/task-detail.json";
import {
  QUEUE_KINDS,
  keypointsOf,
  pickPrediction,
  rejectionReason,
  type LsTask,
} from "./triage";

/**
 * Real Label Studio payloads, captured from a live instance (1.13.1) rather
 * than hand-written.
 *
 * Every other test in this suite mocks `fetch`, which means the *shape* of
 * Label Studio's response is something we assert about ourselves. That blind
 * spot is what shipped the empty queue: `/api/tasks/?project=N` omits
 * `predictions` entirely, so judging the list row refused every task in every
 * project with "no prediction (0 present)" — and four deploys went out before
 * anyone looked at an actual response body.
 *
 * These fixtures are the one thing in the suite that cannot drift from
 * reality, because they were reality. Re-capture them if Label Studio is
 * upgraded; do not edit them by hand.
 */
const EMPTY = new Set<number>();
const LASER = QUEUE_KINDS.laser;

const listedTask = (listFixture as { tasks: LsTask[] }).tasks[0];
const detailTask = detailFixture as unknown as LsTask;

describe("the task LIST response", () => {
  it("carries no predictions and no annotations", () => {
    expect(Object.hasOwn(listedTask, "predictions")).toBe(false);
    expect(Object.hasOwn(listedTask, "annotations")).toBe(false);
  });

  it("does carry is_labeled, which is why the cheap refusal is safe", () => {
    expect(Object.hasOwn(listedTask, "is_labeled")).toBe(true);
  });

  // The bug, pinned. A list row can never be judged directly.
  it("is refused for a missing prediction it may well have", () => {
    expect(rejectionReason(listedTask, LASER, EMPTY)).toMatch(/no prediction/);
  });
});

describe("the task DETAIL response", () => {
  it("carries the predictions the list row omitted", () => {
    expect(detailTask.predictions?.length).toBeGreaterThan(0);
  });

  it("is offerable once hydrated", () => {
    expect(rejectionReason(detailTask, LASER, EMPTY)).toBeNull();
  });

  it("yields the keypoint the sync will read back", () => {
    const [kp] = keypointsOf(pickPrediction(detailTask)!);
    expect(kp.fromName).toBe("laser");
    expect(kp.label).toBe("Red Laser");
    expect(kp.xPercent).toBeCloseTo(57.925);
    expect(kp.yPercent).toBeCloseTo(46.966);
  });
});

describe("the same task, both ways", () => {
  // This asymmetry IS the defect. Stated as one assertion so a future change
  // that judges the list row again fails here rather than in production.
  it("is refused as a list row and accepted as a detail row", () => {
    expect(listedTask.id).toBe(detailTask.id);
    expect(rejectionReason(listedTask, LASER, EMPTY)).not.toBeNull();
    expect(rejectionReason(detailTask, LASER, EMPTY)).toBeNull();
  });
});
