/**
 * A phone capture's server measurement, as the web shows it (the owner,
 * 2026-11-10: the CCFRP lengths, unvalidated). The server measures a phone
 * capture nobody measured the way FishSense Mobile does (fishial's mask, its
 * head and tail, the LiDAR depth at each); nobody has reviewed the result, so
 * it is always marked unvalidated and never counted as a measured fish. Its
 * points are full-frame pixels of the capture's JPEG.
 */
import { apiClient, failure, type Schemas } from "./api/client";
import { millimeters } from "./captures";
import { safeSlug, safeUuid } from "./ids";

export type PhoneMeasurement = Schemas["PhoneMeasurementView"];
export type PhoneLength = Schemas["PhoneLengthView"];

/** The capture's current server measurement; null when the server never
 *  measured it (the API says 404 for "not in your tenant" too). */
export async function getPhoneMeasurement(
  accessToken: string,
  slug: string,
  captureId: string,
): Promise<PhoneMeasurement | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/captures/{capture_id}/phone-measurement",
    { params: { path: { slug: safeSlug(slug), capture_id: safeUuid(captureId, "capture") } } },
  );
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api phone measurement failed", response, error);
}

/** The measurement's mask, as PNG bytes; null when it has none. */
export async function getPhoneMask(
  accessToken: string,
  slug: string,
  captureId: string,
): Promise<ArrayBuffer | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/captures/{capture_id}/phone-measurement/mask",
    {
      params: { path: { slug: safeSlug(slug), capture_id: safeUuid(captureId, "capture") } },
      parseAs: "arrayBuffer",
    },
  );
  if (response.status === 404) return null;
  if (data !== undefined && response.ok) return data as ArrayBuffer;
  throw failure("fishsense-api phone mask failed", response, error);
}

const STATUS: Record<string, string> = {
  measured: "Measured",
  no_fish: "No fish found",
  headtail_failed: "Fish found, head and tail not",
  depth_failed: "No depth at the head or tail",
  image_unavailable: "Photo unavailable",
  decode_failed: "Photo or depth unreadable",
};

export function phoneStatusLabel(status: string): string {
  return STATUS[status] ?? status;
}

/** What a capture card says for its server result; null when there is none. */
export function phoneCardLabel(
  phone: PhoneLength | null | undefined,
): { text: string; unvalidated: boolean } | null {
  if (!phone) return null;
  if (phone.status === "measured" && phone.length_m != null) {
    return { text: `${millimeters(phone.length_m)} mm`, unvalidated: true };
  }
  return { text: phoneStatusLabel(phone.status), unvalidated: false };
}
