// The laser's color, as the web shows it (0072; the owner, 2026-10-24).
// Pinned here:
//  * a dot's color reads as a word, with how clearly the kernel read it;
//  * a dot with no reading says so, rather than guessing;
//  * a mission's counts come per source, and sources that disagree are said
//    to, never resolved by a majority.
import { describe, expect, it } from "vitest";
import { colorLabel, colorRows, sourcesDisagree, type LaserColors } from "./laser-color";

const counts = (red: number, green: number, unread = 0) => ({ red, green, unread });

describe("colorLabel", () => {
  it("is the color, with the margin when there is one", () => {
    expect(colorLabel("red")).toBe("Red");
    expect(colorLabel("green", 0.314)).toBe("Green (margin 0.31)");
  });

  it("says when the dot gave no reading", () => {
    expect(colorLabel(null)).toBe("Not read");
    expect(colorLabel(undefined, 0.5)).toBe("Not read");
  });
});

describe("colorRows", () => {
  it("lists every source, labels first, with its counts", () => {
    const rows = colorRows({
      labels: counts(410, 3),
      predictions: counts(120, 2, 40),
      automatic: counts(0, 0),
    });

    expect(rows).toEqual([
      { source: "Labelers", red: 410, green: 3, unread: 0, total: 413 },
      { source: "Laser stage", red: 120, green: 2, unread: 40, total: 162 },
      { source: "Automatic dot", red: 0, green: 0, unread: 0, total: 0 },
    ]);
  });
});

describe("sourcesDisagree", () => {
  const all = (labels: LaserColors["labels"], predictions = counts(0, 0), automatic = counts(0, 0)) => ({
    labels,
    predictions,
    automatic,
  });

  it("is false when every source that read a color mostly reads the same one", () => {
    expect(sourcesDisagree(all(counts(410, 3), counts(120, 2), counts(50, 1)))).toBe(false);
  });

  it("is true when two sources mostly read different colors", () => {
    expect(sourcesDisagree(all(counts(410, 3), counts(2, 90)))).toBe(true);
  });

  it("ignores a source that read nothing, and a tie", () => {
    expect(sourcesDisagree(all(counts(10, 0), counts(0, 0, 40), counts(3, 3)))).toBe(false);
  });
});
