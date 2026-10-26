// Whose head and tail the review page shows (head/tail v5, 2026-10-26).
//
// Pinned here:
//  * SimCC's keypoints are named as what the length is measured from;
//  * a frame sent to review says why, with the disagreement;
//  * the SAM rule is named where there are no keypoints, and why;
//  * no points, no note.
import { describe, expect, it } from "vitest";
import type { AutomaticFrame } from "./automatic";
import { headTailNote } from "./head-tail-source";

function frame(overrides: Partial<AutomaticFrame> = {}): AutomaticFrame {
  return {
    status: "predicted",
    head: { x: 1800, y: 1500 },
    tail: { x: 2300, y: 1480 },
    head_tail_source: "simcc",
    keypoint_status: "predicted",
    keypoint_disagreement: 0.02,
    ...overrides,
  } as AutomaticFrame;
}

describe("headTailNote", () => {
  it("names SimCC's keypoints as what the length is measured from", () => {
    expect(headTailNote(frame())).toBe(
      "Head and tail from the SimCC keypoint model (v5), which the length is measured from.",
    );
  });

  it("says why a frame was sent to review", () => {
    const f = frame({ keypoint_status: "keypoints_disagree", keypoint_disagreement: 0.234 });

    expect(headTailNote(f)).toBe(
      "Head and tail from the SimCC keypoint model (v5). Sent to review: its length and the mask's extremes differ by 23%, so no length is published until it's checked.",
    );
  });

  it("names the SAM rule where there are no keypoints", () => {
    expect(headTailNote(frame({ head_tail_source: "sam_rule", keypoint_status: null }))).toBe(
      "Head and tail at the mask's extremes (the SAM rule), as on a fish model's frame.",
    );
    expect(headTailNote(frame({ head_tail_source: "sam_rule", keypoint_status: "keypoints_refused" }))).toBe(
      "Head and tail at the mask's extremes (the SAM rule): the keypoint model refused this fish.",
    );
  });

  it("says nothing without points", () => {
    expect(headTailNote(frame({ head_tail_source: null }))).toBeNull();
  });
});
