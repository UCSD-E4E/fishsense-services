/**
 * The fix tool's snap: a dropped head or tail moves to the nearest fish pixel
 * of the mask.
 *
 * Ported from fishsense-mobile lib/services/mask_snap.dart (`snapToMask`),
 * line for line. The owner, 2026-10-21: "we should snap in the same way. this
 * will be reused for fishsense lite too".
 * - The search runs outward in square rings, and the first ring with a hit
 *   wins. On that ring the Euclidean-closest hit wins, and on a tie the first
 *   found (the top row, then the bottom, then the sides).
 * - Nothing within `maxRadius` (256, as the app) is no snap. The reviewer is
 *   asked to drop the point closer to the fish.
 *
 * The snap happens while the reviewer places the point, and they see where it
 * went before saving. Their drop point is kept with the review (`raw`). A
 * saved label is never moved afterwards.
 *
 * The mask is row-major, one byte per pixel, nonzero is fish: the app's
 * layout, and what the automatic track stores as a frame-sized grey PNG
 * (0059). That PNG is the render's size (every one in prod on 2026-10-22,
 * 35,149 of them), so its pixels are the render's.
 */

export type Pixel = { x: number; y: number };

export function snapToMask({
  mask,
  width,
  height,
  x,
  y,
  maxRadius = 256,
}: {
  mask: Uint8Array;
  width: number;
  height: number;
  x: number;
  y: number;
  maxRadius?: number;
}): Pixel | null {
  if (width <= 0 || height <= 0) return null;
  if (mask.length < width * height) return null;

  // Dart's `round()` rounds half away from zero; so does this, for the
  // non-negative pixels a drop on the image has.
  const cx = Math.sign(x) * Math.round(Math.abs(x));
  const cy = Math.sign(y) * Math.round(Math.abs(y));

  const isFish = (px: number, py: number) =>
    px >= 0 && px < width && py >= 0 && py < height && mask[py * width + px] !== 0;

  if (isFish(cx, cy)) return { x: cx, y: cy };

  for (let r = 1; r <= maxRadius; r++) {
    const x0 = cx - r;
    const x1 = cx + r;
    const y0 = cy - r;
    const y1 = cy + r;

    let best: Pixel | null = null;
    let bestD2 = Infinity;
    const consider = (px: number, py: number) => {
      if (!isFish(px, py)) return;
      const d2 = (px - cx) ** 2 + (py - cy) ** 2;
      if (d2 < bestD2) {
        bestD2 = d2;
        best = { x: px, y: py };
      }
    };

    for (let px = x0; px <= x1; px++) {
      consider(px, y0);
      consider(px, y1);
    }
    for (let py = y0 + 1; py < y1; py++) {
      consider(x0, py);
      consider(x1, py);
    }

    if (best) return best;
  }
  return null;
}

/** A decoded mask's RGBA pixels as the snap's one byte per pixel: the grey
 *  PNG's brightness, read from its red channel. */
export function maskFromRgba(rgba: Uint8ClampedArray): Uint8Array {
  const out = new Uint8Array(rgba.length / 4);
  for (let i = 0; i < out.length; i++) out[i] = rgba[i * 4];
  return out;
}
