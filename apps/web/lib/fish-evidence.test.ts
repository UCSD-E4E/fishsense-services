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

const laser = (over: Record<string, unknown> = {}) => ({
  depth_m: 1.8,
  range_m: 1.8,
  calibration_producer: "slate",
  dot: { x: 1925, y: 1310 },
  head: { x: 1600, y: 1125 },
  tail: { x: 2200, y: 1360 },
  ...over,
});
const RENDER = { width: 4014, height: 3016 };

describe("a Lite fish's evidence", () => {
  it("has one measurement, current, and no files the API can serve", () => {
    const e = fishEvidence(detail(), null);

    expect(e.history).toEqual([
      { key: "current", when: "2026-10-01T10:00:00Z", lengthM: 0.686, how: "laser-pinhole 1", current: true },
    ]);
    expect(e.files).toEqual([]);
  });

  // Labeled on v1's JPEG of the same transform; a render is the whole frame,
  // never cropped (the owner, 2026-10-22; the processor's whole-frame test),
  // so the points are drawn on it at their own pixels.
  it("draws its head, tail and laser dot on the render at their pixels", () => {
    const e = fishEvidence(detail({ laser: laser(), photo_size: RENDER } as Partial<ObservationDetail>), null);

    expect(e.overlay).toEqual({
      width: 4014,
      height: 3016,
      snout: { x: 1600, y: 1125 },
      fork: { x: 2200, y: 1360 },
      names: { snout: "Head", fork: "Tail" },
      laser: { x: 1925, y: 1310 },
    });
  });

  it("draws nothing without a render to place the points on", () => {
    expect(fishEvidence(detail({ laser: laser() } as Partial<ObservationDetail>), null).overlay).toBeNull();
  });

  it("draws nothing without both head and tail (a v1 fish not yet linked)", () => {
    const blank = laser({ dot: null, head: null, tail: null });

    expect(
      fishEvidence(detail({ laser: blank, photo_size: RENDER } as Partial<ObservationDetail>), null).overlay,
    ).toBeNull();
  });

  it("draws nothing when a point falls outside the render: it is not the labeled frame", () => {
    const outside = laser({ tail: { x: 4100, y: 1360 } });

    expect(
      fishEvidence(detail({ laser: outside, photo_size: RENDER } as Partial<ObservationDetail>), null).overlay,
    ).toBeNull();
  });

  it("draws head and tail when the laser dot was not labeled", () => {
    const e = fishEvidence(
      detail({ laser: laser({ dot: null }), photo_size: RENDER } as Partial<ObservationDetail>),
      null,
    );

    expect(e.overlay?.laser).toBeNull();
    expect(e.overlay?.names).toEqual({ snout: "Head", fork: "Tail" });
  });
});

describe("a mobile fish's evidence comes from its capture", () => {
  it("draws the current measurement's snout and fork on the mask's frame", () => {
    expect(fishEvidence(detail(), capture()).overlay).toEqual({
      width: 1920,
      height: 1440,
      snout: { x: 100, y: 200 },
      fork: { x: 900, y: 210 },
      names: { snout: "Snout", fork: "Fork" },
      laser: null,
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
