// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/triage.test.ts.
import { describe, expect, it } from "vitest";
import {
  QUEUE_KINDS,
  diveNameFromTitle,
  keypointsOf,
  matchesProjectTitle,
  pickPrediction,
  rejectionReason,
  type LsTask,
} from "./triage";

const LASER = QUEUE_KINDS.laser;

function keypoint(overrides: Record<string, unknown> = {}) {
  return {
    from_name: "laser",
    to_name: "img",
    type: "keypointlabels",
    original_width: 4000,
    original_height: 3000,
    image_rotation: 0,
    value: { x: 57.925, y: 46.966, width: 0.3, keypointlabels: ["Red Laser"] },
    ...overrides,
  };
}

function task(overrides: Partial<LsTask> = {}): LsTask {
  return {
    id: 1,
    is_labeled: false,
    annotations: [],
    predictions: [{ id: 10, result: [keypoint()] }],
    data: { image: "s3://bucket/preprocess_jpeg/abc.JPG" },
    ...overrides,
  };
}

describe("project title vocabulary", () => {
  // Titles are `"{dive.name} #{dive_id} - {suffix}"` — `build_per_dive_title`
  // in the api-worker. The `#{dive_id}` is always present because dive names
  // are NOT unique in prod.
  it("matches a per-dive laser project", () => {
    expect(
      matchesProjectTitle("2024-08-21 reef dive 3 #412 - Laser Calibration Labeling", LASER),
    ).toBe(true);
  });

  it("does not match another stage's project", () => {
    expect(matchesProjectTitle("2024-08-21 reef dive 3 #412 - HeadTail Labeling", LASER)).toBe(
      false,
    );
    expect(matchesProjectTitle("2024-08-21 reef dive 3 #412 - Species Labeling", LASER)).toBe(
      false,
    );
  });

  it("keeps the dive id in the recovered name", () => {
    // The id is what disambiguates two dives sharing a name, so it must not
    // be trimmed off as though it were decoration.
    expect(diveNameFromTitle("2024-08-21 reef dive 3 #412 - Laser Calibration Labeling", LASER)).toBe(
      "2024-08-21 reef dive 3 #412",
    );
  });

  it("handles a nameless dive", () => {
    expect(diveNameFromTitle("#412 - Laser Calibration Labeling", LASER)).toBe("#412");
  });
});

describe("pickPrediction", () => {
  it("takes the newest prediction carrying regions", () => {
    const t = task({
      predictions: [
        { id: 1, result: [] },
        { id: 2, result: [keypoint()] },
      ],
    });
    expect(pickPrediction(t)?.id).toBe(2);
  });

  it("is null when no prediction carries regions", () => {
    expect(pickPrediction(task({ predictions: [{ id: 1, result: [] }] }))).toBeNull();
  });
});

describe("rejectionReason", () => {
  const skips = new Set<number>();

  it("accepts a well-formed pre-annotated task", () => {
    expect(rejectionReason(task(), LASER, skips)).toBeNull();
  });

  it("rejects a task that already has an annotation", () => {
    expect(rejectionReason(task({ annotations: [{ id: 5 }] }), LASER, skips)).toMatch(
      /already annotated/,
    );
  });

  it("rejects a task Label Studio already considers labeled", () => {
    expect(rejectionReason(task({ is_labeled: true }), LASER, skips)).toMatch(/is_labeled/);
  });

  it("rejects a task skipped on this device", () => {
    expect(rejectionReason(task({ id: 77 }), LASER, new Set([77]))).toMatch(/skipped/);
  });

  it("rejects a task with no prediction", () => {
    expect(rejectionReason(task({ predictions: [] }), LASER, skips)).toMatch(/no prediction/);
  });

  // A region on a control the sync activity does not read would vanish on the
  // way back into SQL, so it must never be offered for acceptance.
  it("rejects a prediction on a control the sync ignores", () => {
    const t = task({ predictions: [{ id: 1, result: [keypoint({ from_name: "bbox" })] }] });
    expect(rejectionReason(t, LASER, skips)).toMatch(/from_name/);
  });

  it("accepts the legacy laser control name", () => {
    const t = task({ predictions: [{ id: 1, result: [keypoint({ from_name: "kp-1" })] }] });
    expect(rejectionReason(t, LASER, skips)).toBeNull();
  });

  it("rejects a task whose image is unresolvable", () => {
    expect(rejectionReason(task({ data: {} }), LASER, skips)).toMatch(/image/);
  });

  // Never reject on region count. The accept path copies whatever the model
  // produced, verbatim; a partial detection is surfaced to the labeler, not
  // hidden from them.
  it("accepts a prediction even when it looks partial", () => {
    const t = task({ predictions: [{ id: 1, result: [keypoint()] }] });
    expect(rejectionReason(t, LASER, skips)).toBeNull();
    expect(keypointsOf(pickPrediction(t)!)).toHaveLength(LASER.expectedKeypoints);
  });
});

describe("keypointsOf", () => {
  it("reads Label Studio percentage coordinates", () => {
    const [kp] = keypointsOf({ id: 1, result: [keypoint()] });
    expect(kp.xPercent).toBeCloseTo(57.925);
    expect(kp.yPercent).toBeCloseTo(46.966);
    expect(kp.label).toBe("Red Laser");
    expect(kp.originalWidth).toBe(4000);
  });

  it("ignores non-keypoint regions", () => {
    const region = { from_name: "caption", to_name: "img", type: "textarea", value: { text: ["x"] } };
    expect(keypointsOf({ id: 1, result: [region] })).toHaveLength(0);
  });
});
