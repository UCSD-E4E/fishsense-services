/**
 * Whose head and tail the review page shows (head/tail v5; the owner,
 * 2026-10-26: "switch to this as the primary version").
 *
 * A frame's automatic head and tail are SimCC's keypoints when it has them:
 * what its length is measured from, and what an accept takes. A fish model's
 * frame (no keypoints) and a box the model refused keep the SAM rule's, the
 * mask's extremes. A frame whose two lengths differ by over 15% was sent to
 * review with no published length; the note says so.
 */
import type { AutomaticFrame } from "./automatic";

/** The note under the head and tail; null without points. */
export function headTailNote(frame: AutomaticFrame): string | null {
  if (frame.head_tail_source === "simcc") {
    const simcc = "Head and tail from the SimCC keypoint model (v5)";
    if (frame.keypoint_status !== "keypoints_disagree") {
      return `${simcc}, which the length is measured from.`;
    }
    const pct = Math.round((frame.keypoint_disagreement ?? 0) * 100);
    return (
      `${simcc}. Sent to review: its length and the mask's extremes differ by ${pct}%, ` +
      "so no length is published until it's checked."
    );
  }
  if (frame.head_tail_source === "sam_rule") {
    return frame.keypoint_status === "keypoints_refused"
      ? "Head and tail at the mask's extremes (the SAM rule): the keypoint model refused this fish."
      : "Head and tail at the mask's extremes (the SAM rule), as on a fish model's frame.";
  }
  return null;
}
