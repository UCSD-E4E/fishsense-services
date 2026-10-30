// The fish page's automatic section, read against the measurement above it.
//
// Pinned here: the section says the automatic run agrees with the human
// length when that length is inside its error bar, and how far apart they
// are when it is not -- never that it "isn't the measurement above" (the
// owner, 2026-10-30: mission 383's capture 0dd398f5, 694 ± 16 mm against
// 686 mm, was told so although the two agree).
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { AutomaticFrame } from "@/lib/automatic";
import { AutomaticSection } from "./automatic-section";

function frame(lengthM: number, sdM: number | null): AutomaticFrame {
  return {
    status: "predicted",
    length_m: lengthM,
    length_sd_m: sdM,
    calibration_source: sdM == null ? "stored" : "label_free",
    render_url: null,
  } as unknown as AutomaticFrame;
}

function html(lengthM: number, sdM: number | null, humanM: number | null): string {
  return renderToStaticMarkup(
    createElement(AutomaticSection, { frame: frame(lengthM, sdM), humanLengthM: humanM }),
  );
}

describe("AutomaticSection against the measurement above", () => {
  it("says the run agrees when the human length is inside its error bar", () => {
    const out = html(0.694141, 0.015831, 0.686386);

    expect(out).toContain("agrees with the measurement above, within its error");
    expect(out).not.toContain("the measurement above.</");
    expect(out).toContain("within the error");
  });

  it("says how far apart they are when it is outside", () => {
    const out = html(0.75, 0.01, 0.686386);

    expect(out).toContain("differs from the measurement above");
    expect(out).toContain("outside the error");
  });

  it("claims neither without an error bar", () => {
    const out = html(0.7, null, 0.686386);

    expect(out).not.toContain("agrees");
    expect(out).not.toContain("differs");
    expect(out).toContain("measured separately");
  });
});
