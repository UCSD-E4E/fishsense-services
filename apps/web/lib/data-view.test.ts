import { describe, expect, it } from "vitest";
import { byDay, deploymentFacts, provenance } from "./data-view";
import type { MissionRow } from "./mission-facts";

const mission = (number: number, started_at: string, device_name: string | null, captures: number) =>
  ({ number, started_at, device_name, captures, name: `m${number}`, site: null }) as unknown as MissionRow;

describe("a deployment's missions, by day (docs/design/README.md, Web · Data)", () => {
  it("groups by the camera's own date, in order, missions in start order", () => {
    // Camera times are the camera's local clock stored as UTC, so the UTC
    // date is the dive's date (deployment_inference.py).
    const days = byDay([
      mission(387, "2024-06-26T08:00:00Z", "FSL-06", 970),
      mission(374, "2024-06-23T08:00:00Z", "FSL-03", 37),
      mission(383, "2024-06-26T07:00:00Z", "FSL-03", 40),
    ]);

    expect(days.map((d) => [d.date, d.missions.map((m) => m.number)])).toEqual([
      ["2024-06-23", [374]],
      ["2024-06-26", [383, 387]],
    ]);
  });

  it("labels each day for people", () => {
    expect(byDay([mission(1, "2024-06-26T07:00:00Z", null, 0)])[0].label).toBe("Wed 26 Jun 2024");
  });
});

describe("what a deployment holds", () => {
  it("counts its frames and names its cameras", () => {
    expect(
      deploymentFacts([
        mission(1, "2024-06-23T08:00:00Z", "FSL-06", 970),
        mission(2, "2024-06-23T09:00:00Z", "FSL-03", 37),
        mission(3, "2024-06-24T09:00:00Z", "FSL-03", 40),
        mission(4, "2024-06-24T10:00:00Z", null, 3),
      ]),
    ).toEqual({ missions: 4, frames: 1050, cameras: ["FSL-03", "FSL-06"] });
  });
});

describe("how a deployment was made", () => {
  it("names an e4edm dataset by its folder", () => {
    expect(provenance({ source_path: "/fishsense_data/2025.01.17.FishSense.San Diego", notes: null })).toEqual({
      kind: "dataset",
      label: "From its e4edm dataset",
    });
  });

  it("says when it was inferred from folder names and dates", () => {
    const notes =
      "inferred from folder names and capture dates (the owner's rules, 2026-10-20): 6 sessions, 12 missions";
    expect(provenance({ source_path: null, notes })).toEqual({
      kind: "inferred",
      label: "Inferred from folder names and dates",
    });
  });

  it("says nothing it does not know", () => {
    expect(provenance({ source_path: null, notes: "A trip someone typed in" })).toBeNull();
  });
});
