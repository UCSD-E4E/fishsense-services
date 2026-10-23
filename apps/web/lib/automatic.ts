/**
 * The automatic track, as the web shows it for review (the owner, 2026-10-21:
 * "use this app to review the auto process").
 *
 * Read-only, for any member of the tenant, always marked automatic and
 * unvalidated: these are a model's results, kept apart from the measurements.
 * A frame's points are full-frame pixels of the render they were predicted
 * on, which the API names, so they are drawn on that image at its size. A
 * mission's lengths are compared with the human lengths on the same frames:
 * agreement with the human path, not error against a known length.
 */
import { apiClient, failure, type Schemas } from "./api/client";
import { safeSlug, safeUuid } from "./ids";

export type AutomaticFrame = Schemas["AutomaticFrameView"];
export type MissionAutomatic = Schemas["MissionAutomaticView"];

type Point = { x: number; y: number };

/** What the automatic run made of a frame; null when it never reached it
 *  (the API says 404 for "not in your tenant" too). */
export async function getAutomaticFrame(
  accessToken: string,
  slug: string,
  captureId: string,
): Promise<AutomaticFrame | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/captures/{capture_id}/automatic",
    {
      params: {
        path: { slug: safeSlug(slug), capture_id: safeUuid(captureId, "capture") },
      },
    },
  );
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api automatic frame failed", response, error);
}

/** A mission's automatic run; null for a mission the caller can't see. */
export async function getMissionAutomatic(
  accessToken: string,
  slug: string,
  number: number,
): Promise<MissionAutomatic | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/missions/{number}/automatic",
    { params: { path: { slug: safeSlug(slug), number } } },
  );
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api automatic run failed", response, error);
}

const STATUS: Record<string, string> = {
  predicted: "Head and tail found",
  no_laser_dot: "No laser dot found",
  no_detections: "No fish found",
  laser_off_all_fish: "Laser dot not on a fish",
  headtail_failed: "Fish found, head and tail not",
  decode_failed: "The photo would not decode",
  raw_unavailable: "The raw photo was not there",
  slate_frame: "A slate frame",
  frame_failed: "Failed three times",
  mask_unscored: "The mask was not scored",
};

/** A frame's automatic status, as words. */
export function statusLabel(status: string): string {
  return STATUS[status] ?? status;
}

/** The automatic points over their own render; null without that render. */
export function automaticOverlay(frame: AutomaticFrame): {
  width: number;
  height: number;
  head: Point | null;
  tail: Point | null;
  laser: Point | null;
  maskUrl: string | null;
} | null {
  if (!frame.render_url || !frame.render_width || !frame.render_height) return null;
  return {
    width: frame.render_width,
    height: frame.render_height,
    head: frame.head ?? null,
    tail: frame.tail ?? null,
    laser: frame.laser ?? null,
    maskUrl: frame.mask_url ?? null,
  };
}

/** How a mission's automatic lengths agree with the human ones. */
export function agreement(run: MissionAutomatic): string {
  if (!run.compared || run.median_error_pct == null) {
    return "No frame has both an automatic and a human length yet";
  }
  const median = `${run.median_error_pct >= 0 ? "+" : ""}${run.median_error_pct.toFixed(1)}%`;
  return (
    `Median ${median} against the human length, ` +
    `${run.within_5_pct} of ${run.compared} within 5%, ${run.within_10_pct} within 10%`
  );
}

/** The frame's current mask, as its PNG bytes; null when it has none. The
 *  review tool snaps to its pixels, which a page can read only from its own
 *  origin: app/api/masks passes these on. */
export async function getAutomaticMask(
  accessToken: string,
  slug: string,
  captureId: string,
): Promise<ArrayBuffer | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/captures/{capture_id}/automatic/mask",
    {
      params: {
        path: { slug: safeSlug(slug), capture_id: safeUuid(captureId, "capture") },
      },
      parseAs: "arrayBuffer",
    },
  );
  if (response.status === 404) return null;
  if (data !== undefined && response.ok) return data as ArrayBuffer;
  throw failure("fishsense-api automatic mask failed", response, error);
}
