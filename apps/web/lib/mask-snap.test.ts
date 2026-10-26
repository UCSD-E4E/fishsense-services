// The fix tool's snap, ported from fishsense-mobile lib/services/mask_snap.dart
// (the owner, 2026-10-21: "we should snap in the same way"). Pinned here, as
// the app behaves:
//  * a drop on the fish stays where it is, rounded to its pixel;
//  * otherwise the nearest fish pixel by square rings: the first ring with a
//    hit wins, and on it the Euclidean-closest, the first found on a tie;
//  * nothing within 256 px is no snap (null): the reviewer drops closer;
//  * a mask that doesn't fit its size is no snap.
import { describe, expect, it } from "vitest";
import { maskFromRgba, snapToMask } from "./mask-snap";

function mask(width: number, height: number, fish: [number, number][]): Uint8Array {
  const m = new Uint8Array(width * height);
  for (const [x, y] of fish) m[y * width + x] = 255;
  return m;
}

describe("snapToMask", () => {
  it("keeps a drop on the fish, at its pixel", () => {
    const m = mask(10, 10, [[4, 5]]);
    expect(snapToMask({ mask: m, width: 10, height: 10, x: 4.4, y: 4.6 })).toEqual({ x: 4, y: 5 });
  });

  it("moves a drop off the fish to the nearest fish pixel", () => {
    const m = mask(20, 20, [[10, 3], [12, 12]]);
    expect(snapToMask({ mask: m, width: 20, height: 20, x: 10, y: 10 })).toEqual({ x: 12, y: 12 });
  });

  it("takes the first ring with a hit, then the closest on it", () => {
    // Ring 3 holds (13, 10) at distance 3 and (13, 13) at 4.24: the edge wins.
    const m = mask(20, 20, [[13, 13], [13, 10]]);
    expect(snapToMask({ mask: m, width: 20, height: 20, x: 10, y: 10 })).toEqual({ x: 13, y: 10 });
    // A corner of ring 2 (distance 2.83) beats an edge of ring 3 (distance 3).
    const n = mask(20, 20, [[12, 12], [13, 10]]);
    expect(snapToMask({ mask: n, width: 20, height: 20, x: 10, y: 10 })).toEqual({ x: 12, y: 12 });
  });

  it("breaks a tie as the app does: the first found, top row first", () => {
    const m = mask(20, 20, [[10, 12], [10, 8]]);
    expect(snapToMask({ mask: m, width: 20, height: 20, x: 10, y: 10 })).toEqual({ x: 10, y: 8 });
  });

  it("is no snap with no fish within the radius", () => {
    const m = mask(600, 10, [[400, 5]]);
    expect(snapToMask({ mask: m, width: 600, height: 10, x: 10, y: 5 })).toBeNull();
    expect(snapToMask({ mask: m, width: 600, height: 10, x: 10, y: 5, maxRadius: 400 })).toEqual({ x: 400, y: 5 });
  });

  it("reaches exactly 256 pixels, as the app does, and no further", () => {
    const at256 = mask(600, 3, [[266, 1]]);
    expect(snapToMask({ mask: at256, width: 600, height: 3, x: 10, y: 1 })).toEqual({ x: 266, y: 1 });
    const at257 = mask(600, 3, [[267, 1]]);
    expect(snapToMask({ mask: at257, width: 600, height: 3, x: 10, y: 1 })).toBeNull();
  });

  it("is no snap for a mask that doesn't fit its size", () => {
    expect(snapToMask({ mask: new Uint8Array(5), width: 10, height: 10, x: 1, y: 1 })).toBeNull();
    expect(snapToMask({ mask: new Uint8Array(0), width: 0, height: 0, x: 0, y: 0 })).toBeNull();
  });
});

describe("maskFromRgba", () => {
  it("reads the gray PNG's brightness: nonzero is fish", () => {
    const rgba = new Uint8ClampedArray([0, 0, 0, 255, 255, 255, 255, 255, 1, 1, 1, 255]);
    expect(Array.from(maskFromRgba(rgba))).toEqual([0, 255, 1]);
  });
});
