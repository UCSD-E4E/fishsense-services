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

export type CapturePage = Schemas["CapturePage"];
export type CaptureSummary = Schemas["CaptureSummaryView"];
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

/** One page of the tenant's captures, newest first. */
export async function getCapturePage(
  accessToken: string,
  slug: string,
  before?: string,
): Promise<CapturePage> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/mobile/captures",
    {
      params: {
        path: { slug: safeSlug(slug) },
        query: before ? { before } : {},
      },
    },
  );
  if (data === undefined)
    throw failure("fishsense-api captures failed", response, error);
  return data;
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

export function formatLength(lengthM: number | null | undefined): string {
  return lengthM == null ? "not measured" : `${(lengthM * 100).toFixed(1)} cm`;
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
