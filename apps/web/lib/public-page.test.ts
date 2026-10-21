import { describe, expect, it } from "vitest";
import { MAP, project, regionLabel } from "./public-page";

describe("the public map's projection matches public/world.svg", () => {
  it("is equirectangular from 84°N to 58°S", () => {
    expect(MAP).toEqual({ width: 1200, north: 84, south: -58 });
    expect(project(84, -180)).toEqual({ left: 0, top: 0 });
    expect(project(-58, 180)).toEqual({ left: 100, top: 100 });
  });

  it("puts the Florida Keys region where the design drew it", () => {
    const { left, top } = project(27.0, -81.5);
    expect(left).toBeCloseTo(27.36, 2);
    expect(top).toBeCloseTo(40.14, 2);
  });
});

describe("a region says no more than it may", () => {
  it("rounds a count it shows", () => {
    expect(regionLabel({ count: 200, under: null })).toBe("about 200 fish");
    expect(regionLabel({ count: 1500, under: null })).toBe("about 1,500 fish");
  });

  it("says only 'under' below the minimum", () => {
    expect(regionLabel({ count: null, under: 25 })).toBe("under 25 fish");
  });
});
