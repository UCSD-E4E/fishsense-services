// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/triage-view.ts.
/**
 * Where a frame sits under the viewport, and how zoom carries between frames.
 *
 * Split out of the viewer component because it is arithmetic, not rendering:
 * the viewer writes one `transform` string per animation frame and holds no
 * React state, so a mistake here is invisible to anything that inspects the
 * DOM and shows up only as a frame that opens looking at the wrong place.
 */

/**
 * Deliberately very high. A 4000px-wide frame fitted to a phone screen puts one
 * image pixel at roughly 0.1 CSS px, so reaching a pixel you can actually look
 * at needs a scale near 100. Verified on device: at 120x with dpr 2.81 the
 * frame stays hard-edged, one image pixel covering ~160 device pixels.
 */
export const MAX_SCALE = 120;
export const MIN_SCALE = 0.02;

export type Box = { width: number; height: number };
export type ViewPoint = { xPercent: number; yPercent: number };
export type View = { k: number; tx: number; ty: number };

/** The scale at which the whole frame is visible: whichever axis binds first. */
export function fitScale(stage: Box, image: Box): number {
  return Math.min(stage.width / image.width, stage.height / image.height);
}

export function clampScale(k: number): number {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, k));
}

/**
 * The view a frame opens at.
 *
 * With no retained scale this is the familiar fit-and-centre. With one, the
 * magnification is kept and the view is re-centred on **this** frame's
 * prediction.
 *
 * Carrying the previous frame's pan across as well would be the more literal
 * reading of "remember the zoom", and it is the wrong one: consecutive tasks
 * are unrelated frames whose dots are in unrelated places, so at 120x the
 * labeler would open every frame looking at whatever water the *previous* dot
 * happened to sit in, and have to hunt for the real one at maximum
 * magnification. Re-centring is what makes retained zoom worth having — the
 * frame opens already looking at the thing being judged.
 */
export function initialView({
  stage,
  image,
  keypoint,
  retainedScale,
}: {
  stage: Box;
  image: Box;
  keypoint: ViewPoint | null;
  retainedScale: number | null;
}): View {
  const fit = fitScale(stage, image);
  // At or below fit scale the whole frame is on screen, so there is nothing to
  // centre on and re-centring would only push part of it off the stage.
  // Zooming out is how a labeler asks for context; it must not be sticky.
  if (retainedScale === null || retainedScale <= fit) {
    const k = fit;
    return {
      k,
      tx: (stage.width - image.width * k) / 2,
      ty: (stage.height - image.height * k) / 2,
    };
  }

  const k = clampScale(retainedScale);
  // A prediction with no region still has to be lookable-at, so fall back to
  // the middle of the frame rather than refusing to place it.
  const focus = keypoint ?? { xPercent: 50, yPercent: 50 };
  const ix = (focus.xPercent / 100) * image.width;
  const iy = (focus.yPercent / 100) * image.height;
  return {
    k,
    tx: stage.width / 2 - ix * k,
    ty: stage.height / 2 - iy * k,
  };
}
