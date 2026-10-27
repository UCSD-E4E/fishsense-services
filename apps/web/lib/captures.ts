/**
 * FishSense Mobile captures, as the web shows them (new in v2; PLAN.md §9.6).
 *
 * Every call is made as the signed-in user, so the API decides what they may
 * see: a member sees their own tenant's captures, a partner their org's. The
 * web only ever asks for a tenant the caller belongs to (`chooseTenant` over
 * `GET /me/memberships`), and a slug or capture id that came from the URL is
 * validated before it goes into an API path.
 */
import { apiClient, failure, type Schemas } from "./api/client";
import type { TenantMembership } from "./fishsense-api";
import { safeSlug, safeUuid } from "./ids";
import { paths } from "./tenant-paths";

export type Capture = Schemas["CaptureView"];
export type Measurement = Schemas["MeasurementView"];

/** The tenant to show: the requested one if the user is in it, else their
 *  first; null for a user in none. */
export function chooseTenant(
  tenants: TenantMembership[],
  requested: string | undefined,
): TenantMembership | null {
  return tenants.find((t) => t.slug === requested) ?? tenants[0] ?? null;
}

/** Where an old link to a capture goes: its fish's page (phase 7b, when the
 *  capture list was retired); null for a capture without a fish. */
export function fishOfCapture(
  slug: string,
  capture: { capture_id: string; fish_id?: string | null },
): string | null {
  return capture.fish_id ? paths.fishDetail(slug, capture.capture_id, capture.fish_id) : null;
}

/** One capture with everything the phone saved; null when there is none
 *  (the API says 404 for "not in your tenant" too). */
export async function getCapture(
  accessToken: string,
  slug: string,
  captureId: string,
): Promise<Capture | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/mobile/captures/{client_capture_uuid}",
    {
      params: {
        path: {
          slug: safeSlug(slug),
          client_capture_uuid: safeUuid(captureId, "capture"),
        },
      },
    },
  );
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api capture failed", response, error);
}

/**
 * A fish's length in whole millimeters, the unit every length is shown in
 * (the owner, 2026-10-21; docs/design/README.md). Stored and served in meters;
 * distances (laser range, GPS accuracy) stay in meters.
 */
export function millimeters(lengthM: number | null | undefined): string {
  return lengthM == null ? "—" : Math.round(lengthM * 1000).toLocaleString("en-US");
}

export function formatLength(lengthM: number | null | undefined): string {
  return lengthM == null ? "not measured" : `${millimeters(lengthM)} mm`;
}

/**
 * A length with its calibration's error bar, 1 SD (0079; the owner,
 * 2026-10-26: a weak calibration answers with its error rather than refusing,
 * and the length is always shown with it). Whole millimeters, never under 1;
 * the plain length where no error bar was measured.
 */
export function formatLengthWithError(
  lengthM: number | null | undefined,
  sdM: number | null | undefined,
): string {
  if (lengthM == null || sdM == null) return formatLength(lengthM);
  return `${millimeters(lengthM)} ± ${Math.max(1, Math.round(sdM * 1000)).toLocaleString("en-US")} mm`;
}

export function osmLink(latitude: number, longitude: number): string {
  return `https://www.openstreetmap.org/?mlat=${latitude}&mlon=${longitude}#map=17/${latitude}/${longitude}`;
}

type Point = { x: number; y: number };

/** Snout and fork in the RGB frame's pixel space -- the mask's, which the
 *  phone segments at the frame's resolution -- for an SVG over the image. */
export function measurementOverlay(
  capture: { mask?: { width: number; height: number } | null },
  measurement: { snout: Point; fork: Point } | null | undefined,
): { width: number; height: number; snout: Point; fork: Point } | null {
  if (!measurement || !capture.mask) return null;
  return {
    width: capture.mask.width,
    height: capture.mask.height,
    snout: measurement.snout,
    fork: measurement.fork,
  };
}

/** Who placed a measurement's snout and fork points. */
export function describeOrigin(origin: string): string {
  switch (origin) {
    case "auto":
      return "found by the phone";
    case "manual":
      return "placed by the user";
    default:
      // `unknown`: the app's v7 overwrote edits in place (fishsense-mobile #68).
      return "not recorded (before app v8)";
  }
}

/** Where a capture was taken, by name: the app's, else the one the server
 *  derived from its coordinates on our own geocoder (marked as such). */
export function placeName(capture: {
  place_name?: string | null;
  derived_place_name?: string | null;
}): { name: string; derived: boolean } | null {
  if (capture.place_name) return { name: capture.place_name, derived: false };
  if (capture.derived_place_name) {
    return { name: capture.derived_place_name, derived: true };
  }
  return null;
}
