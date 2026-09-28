// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/triage-view.test.ts.
import { describe, expect, it } from "vitest";
import { MAX_SCALE, MIN_SCALE, clampScale, fitScale, initialView } from "./triage-view";

/** A phone-ish stage and a real Olympus frame. */
const STAGE = { width: 390, height: 292.5 };
const IMAGE = { width: 4000, height: 3000 };

/** Dead centre of the frame, and a dot up and to the left of it. */
const CENTRED = { xPercent: 50, yPercent: 50 };
const OFF_CENTRE = { xPercent: 25, yPercent: 80 };

/** Where an image point lands on screen under a computed view. */
function project(
  view: { k: number; tx: number; ty: number },
  kp: { xPercent: number; yPercent: number },
) {
  return {
    x: view.tx + (kp.xPercent / 100) * IMAGE.width * view.k,
    y: view.ty + (kp.yPercent / 100) * IMAGE.height * view.k,
  };
}

describe("with no zoom carried in", () => {
  it("fits the whole frame", () => {
    const view = initialView({ stage: STAGE, image: IMAGE, keypoint: OFF_CENTRE, retainedScale: null });
    expect(view.k).toBeCloseTo(fitScale(STAGE, IMAGE));
  });

  // The fitted frame is centred on the STAGE, which is the one case where the
  // keypoint deliberately does not drive the view: at fit scale the whole
  // frame is visible, so there is nothing to centre on.
  it("centres the frame, not the prediction", () => {
    const view = initialView({ stage: STAGE, image: IMAGE, keypoint: OFF_CENTRE, retainedScale: null });
    const centre = project(view, CENTRED);
    expect(centre.x).toBeCloseTo(STAGE.width / 2);
    expect(centre.y).toBeCloseTo(STAGE.height / 2);
  });
});

describe("with zoom carried in from the previous frame", () => {
  /**
   * This is the whole point of the feature. Preserving the previous frame's
   * pan as well would land a 120x view on whatever patch of water the LAST
   * dot occupied — the new dot is somewhere else entirely, so the labeler
   * would have to hunt for it at maximum magnification. Preserving the
   * magnification and re-centring is what makes the retained zoom useful.
   */
  it("keeps the magnification", () => {
    const view = initialView({ stage: STAGE, image: IMAGE, keypoint: OFF_CENTRE, retainedScale: 120 });
    expect(view.k).toBe(120);
  });

  it("puts the new frame's prediction under the middle of the stage", () => {
    const view = initialView({ stage: STAGE, image: IMAGE, keypoint: OFF_CENTRE, retainedScale: 120 });
    const dot = project(view, OFF_CENTRE);
    expect(dot.x).toBeCloseTo(STAGE.width / 2);
    expect(dot.y).toBeCloseTo(STAGE.height / 2);
  });

  it("centres each frame's own prediction, not a fixed point", () => {
    const a = initialView({ stage: STAGE, image: IMAGE, keypoint: OFF_CENTRE, retainedScale: 120 });
    const b = initialView({ stage: STAGE, image: IMAGE, keypoint: CENTRED, retainedScale: 120 });
    expect(a.tx).not.toBeCloseTo(b.tx);
    expect(project(b, CENTRED).x).toBeCloseTo(STAGE.width / 2);
  });

  it("falls back to the frame centre when a prediction has no keypoint", () => {
    const view = initialView({ stage: STAGE, image: IMAGE, keypoint: null, retainedScale: 120 });
    expect(view.k).toBe(120);
    expect(project(view, CENTRED).x).toBeCloseTo(STAGE.width / 2);
  });

  /**
   * Zooming out to see the whole frame is how a labeler asks for context, and
   * at or below fit scale there is nothing to centre on — re-centring would
   * only push part of the frame off the stage.
   */
  it("fits instead of re-centring when the carried scale shows the whole frame", () => {
    const fit = fitScale(STAGE, IMAGE);
    const view = initialView({ stage: STAGE, image: IMAGE, keypoint: OFF_CENTRE, retainedScale: fit });
    const centre = project(view, CENTRED);
    expect(view.k).toBeCloseTo(fit);
    expect(centre.x).toBeCloseTo(STAGE.width / 2);
    expect(centre.y).toBeCloseTo(STAGE.height / 2);
  });

  it("clamps a carried scale to the supported range", () => {
    const hot = initialView({ stage: STAGE, image: IMAGE, keypoint: CENTRED, retainedScale: 10_000 });
    expect(hot.k).toBe(MAX_SCALE);
    // No cold twin here: anything below fit is caught by the fit rule above
    // long before it could reach MIN_SCALE. That floor governs live pinching,
    // where a labeler CAN push past fit, so it is tested on clampScale.
  });
});

describe("clampScale", () => {
  it("holds the live pinch inside the supported range", () => {
    expect(clampScale(1e-9)).toBe(MIN_SCALE);
    expect(clampScale(10_000)).toBe(MAX_SCALE);
    expect(clampScale(12)).toBe(12);
  });
});

describe("fitScale", () => {
  // Whichever axis runs out first — a 4:3 frame in a wider stage is height-bound.
  it("uses the tighter axis so the whole frame fits", () => {
    expect(fitScale({ width: 4000, height: 10_000 }, IMAGE)).toBeCloseTo(1);
    expect(fitScale({ width: 10_000, height: 3000 }, IMAGE)).toBeCloseTo(1);
  });
});
