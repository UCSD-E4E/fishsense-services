import { describe, expect, it } from "vitest";
import { fishEvidence, type CaptureEvidence } from "./fish-evidence";
import type { ObservationDetail } from "./observations";

const detail = (over: Partial<ObservationDetail> = {}): ObservationDetail =>
  ({
    algorithm: "laser-pinhole",
    algorithm_version: "1",
    core_version: "0.9.0",
    device_points: null,
    laser: null,
    measured_at: "2026-10-01T10:00:00Z",
    model_version: null,
    observation: { length_m: 0.686, client_capture_uuid: null },
    ...over,
  }) as unknown as ObservationDetail;

const measurement = (at: string, length: number) => ({
  client_measurement_uuid: `m-${at}`,
  measured_at: at,
  length_m: length,
  origin: "auto",
  algorithm: "lidar",
  algorithm_version: "2",
  model_version: null,
  core_version: null,
  snout: { x: 100, y: 200 },
  fork: { x: 900, y: 210 },
});

const capture = (over: Partial<CaptureEvidence> = {}): CaptureEvidence =>
  ({
    mask: { width: 1920, height: 1440 },
    current_measurement: measurement("2026-10-02T00:00:00Z", 0.41),
    measurements: [
      measurement("2026-10-01T00:00:00Z", 0.4),
      measurement("2026-10-02T00:00:00Z", 0.41),
    ],
    files: [
      { role: "mask_u8", size: 2048, sha256: "b".repeat(64), url: "https://x/m" },
      { role: "rgb_jpeg", size: 512_000, sha256: "a".repeat(64), url: "https://x/p" },
    ],
    ...over,
  }) as unknown as CaptureEvidence;

describe("a Lite fish's evidence", () => {
  it("has one measurement, current, and no files the API can serve", () => {
    const e = fishEvidence(detail(), null);

    expect(e.history).toEqual([
      { key: "current", when: "2026-10-01T10:00:00Z", lengthM: 0.686, how: "laser-pinhole 1", current: true },
    ]);
    expect(e.files).toEqual([]);
    // Its labelled points were placed on v1's JPEG, not this render: listed,
    // never drawn (PLAN.md §9.26, Phase 4).
    expect(e.overlay).toBeNull();
  });
});

describe("a mobile fish's evidence comes from its capture", () => {
  it("draws the current measurement's snout and fork on the mask's frame", () => {
    expect(fishEvidence(detail(), capture()).overlay).toEqual({
      width: 1920,
      height: 1440,
      snout: { x: 100, y: 200 },
      fork: { x: 900, y: 210 },
    });
  });

  it("lists every measurement version, newest first, the current one marked", () => {
    const e = fishEvidence(detail(), capture());

    expect(e.history.map((h) => [h.when, h.lengthM, h.current])).toEqual([
      ["2026-10-02T00:00:00Z", 0.41, true],
      ["2026-10-01T00:00:00Z", 0.4, false],
    ]);
  });

  it("lists every synced file, the photo first, with its size and checksum", () => {
    const e = fishEvidence(detail(), capture());

    expect(e.files).toEqual([
      { label: "Photo (JPEG)", size: "500 KiB", checksum: `SHA-256 ${"a".repeat(64)}`, url: "https://x/p" },
      { label: "Fish mask (uint8)", size: "2 KiB", checksum: `SHA-256 ${"b".repeat(64)}`, url: "https://x/m" },
    ]);
  });

  it("draws nothing without the mask's frame to place the points on", () => {
    expect(fishEvidence(detail(), capture({ mask: null })).overlay).toBeNull();
  });
});
