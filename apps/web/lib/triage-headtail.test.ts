// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/triage-headtail.test.ts.
import { describe, expect, it } from "vitest";
import { QUEUE_KINDS, rejectionReason, type LsTask } from "./triage";

/**
 * Head/tail triage, and the one rule that is not shared with laser.
 *
 * `sync_headtail_labels_for_label_studio_project_activity` needs BOTH points:
 * it filters `from_name == "kp-1"`, picks the two regions out by their
 * `keypointlabels[0]` (`Snout` and `Fork`), and only writes x/y when it found
 * both. But `completed = task.is_labeled` flips true regardless — and
 * `_select_unlabeled_images` then excludes that image for good.
 *
 * So accepting a half prediction is not "a slightly worse label", it is the
 * same permanent-exclusion trap that is the reason Skip writes nothing. One
 * point must be refused outright rather than offered with a warning.
 *
 * The populate emits either zero regions or exactly two, so this should not
 * occur today. That is a reason to encode the rule here, not a reason to rely
 * on the far side of an HTTP boundary continuing to hold it.
 */
const EMPTY = new Set<number>();
const HEADTAIL = QUEUE_KINDS.headtail;
const LASER = QUEUE_KINDS.laser;

function point(label: string, fromName = "kp-1") {
  return {
    from_name: fromName,
    to_name: "img-1",
    type: "keypointlabels",
    original_width: 4000,
    original_height: 3000,
    value: { x: 50, y: 50, keypointlabels: [label] },
  };
}

function task(regions: ReturnType<typeof point>[]): LsTask {
  return {
    id: 1,
    is_labeled: false,
    annotations: [],
    predictions: [{ id: 9, result: regions }],
    data: { image: "s3://bucket/preprocess_headtail_jpeg/abc.JPG" },
  };
}

describe("the head/tail kind", () => {
  it("names the vocabulary the sync activity actually reads", () => {
    expect(HEADTAIL.fromNames).toContain("kp-1");
    expect(HEADTAIL.expectedKeypoints).toBe(2);
    expect(HEADTAIL.titleSuffix).toBe("HeadTail Labeling");
  });

  it("offers a prediction carrying both points", () => {
    expect(rejectionReason(task([point("Snout"), point("Fork")]), HEADTAIL, EMPTY)).toBeNull();
  });

  it("refuses a prediction with only the snout", () => {
    const reason = rejectionReason(task([point("Snout")]), HEADTAIL, EMPTY);
    expect(reason).toMatch(/Fork/);
  });

  it("refuses a prediction with only the fork", () => {
    const reason = rejectionReason(task([point("Fork")]), HEADTAIL, EMPTY);
    expect(reason).toMatch(/Snout/);
  });

  // Two regions, wrong ones. A count check alone would wave this through.
  it("refuses two points that are the same end of the fish", () => {
    const reason = rejectionReason(task([point("Snout"), point("Snout")]), HEADTAIL, EMPTY);
    expect(reason).toMatch(/Fork/);
  });

  it("still refuses a region on a control the sync does not read", () => {
    const reason = rejectionReason(
      task([point("Snout", "laser"), point("Fork", "laser")]),
      HEADTAIL,
      EMPTY,
    );
    expect(reason).toMatch(/from_name/);
  });
});

describe("laser is unaffected", () => {
  // Laser labels vary ("Red Laser" / "Green Laser"), so the kind requires a
  // point on the right control and says nothing about which label it carries.
  it("accepts its single point whatever the label says", () => {
    const red = task([point("Red Laser", "laser")]);
    const green = task([point("Green Laser", "kp-1")]);
    expect(rejectionReason(red, LASER, EMPTY)).toBeNull();
    expect(rejectionReason(green, LASER, EMPTY)).toBeNull();
  });
});
