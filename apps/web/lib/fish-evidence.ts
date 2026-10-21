/**
 * What a fish's page shows as the evidence for its length (docs/design/README.md,
 * "One fish, with its evidence"): the points drawn on the photo, every version
 * of the measurement, and every file behind it with its checksum.
 *
 * A mobile fish's evidence is its capture's (the phone's photo, mask, depth and
 * measurement versions), so its page needs no separate Mobile captures page.
 * A Lite fish has one current measurement here; its labelled points are listed,
 * not drawn, because they were placed on v1's JPEG, not on this render.
 */
import type { Capture } from "./captures";
import type { ObservationDetail } from "./observations";

export type CaptureEvidence = Pick<Capture, "mask" | "current_measurement" | "measurements" | "files">;

type Point = { x: number; y: number };

export type Overlay = { width: number; height: number; snout: Point; fork: Point };

export type HistoryRow = {
  key: string;
  when: string;
  lengthM: number | null;
  how: string;
  current: boolean;
};

export type FileRow = { label: string; size: string; checksum: string; url: string | null };

export type FishEvidence = { overlay: Overlay | null; history: HistoryRow[]; files: FileRow[] };

/** In the order a person reproducing the length reads them. */
const FILES: Record<string, string> = {
  rgb_jpeg: "Photo (JPEG)",
  rgb_lossless: "Frame the phone measured on (lossless)",
  depth_f32: "LiDAR depth (float32, metres)",
  confidence_u8: "Depth confidence (uint8)",
  mask_u8: "Fish mask (uint8)",
};
const ORDER = Object.keys(FILES);

function how(algorithm: string | null, version: string | null): string {
  return algorithm ? `${algorithm}${version ? ` ${version}` : ""}` : "not recorded";
}

function kib(bytes: number): string {
  return `${Math.max(1, Math.round(bytes / 1024)).toLocaleString("en-US")} KiB`;
}

export function fishEvidence(
  detail: ObservationDetail,
  capture: CaptureEvidence | null,
): FishEvidence {
  if (!capture) {
    return {
      overlay: null,
      history: [
        {
          key: "current",
          when: detail.measured_at,
          lengthM: detail.observation.length_m,
          how: how(detail.algorithm, detail.algorithm_version),
          current: true,
        },
      ],
      files: [],
    };
  }
  const current = capture.current_measurement;
  const overlay =
    current && capture.mask
      ? { width: capture.mask.width, height: capture.mask.height, snout: current.snout, fork: current.fork }
      : null;
  const history = [...capture.measurements]
    .sort((a, b) => b.measured_at.localeCompare(a.measured_at))
    .map((m) => ({
      key: m.client_measurement_uuid,
      when: m.measured_at,
      lengthM: m.length_m,
      how: how(m.algorithm, m.algorithm_version),
      current: m.client_measurement_uuid === current?.client_measurement_uuid,
    }));
  const rank = (role: string) => (ORDER.includes(role) ? ORDER.indexOf(role) : ORDER.length);
  const files = [...capture.files]
    .sort((a, b) => rank(a.role) - rank(b.role))
    .map((f) => ({
      label: FILES[f.role] ?? f.role,
      size: kib(f.size),
      checksum: `SHA-256 ${f.sha256}`,
      url: f.url ?? null,
    }));
  return { overlay, history, files };
}
