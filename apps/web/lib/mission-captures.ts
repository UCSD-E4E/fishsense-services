/**
 * A mission's captures, measured or not (2026-10-21). The Fish list is
 * measured fish only, so an unmeasured capture -- a phone capture that synced
 * without a length, or the imported CCFRP photos -- is seen on its mission's
 * page or nowhere.
 */
import { apiClient, failure, type Schemas } from "./api/client";
import { safeSlug } from "./ids";
import { paths } from "./tenant-paths";

export type CaptureCardPage = Schemas["CaptureCardPage"];
export type CaptureCard = Schemas["CaptureCardView"];

/** One page of the mission's captures, in the order taken; null for a
 *  mission the caller can't see. */
export async function getMissionCaptures(
  accessToken: string,
  slug: string,
  number: number,
  after?: string,
  limit?: number,
): Promise<CaptureCardPage | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/missions/{number}/captures",
    {
      params: {
        path: { slug: safeSlug(slug), number },
        query: { ...(after ? { after } : {}), ...(limit ? { limit } : {}) },
      },
    },
  );
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api mission captures failed", response, error);
}

/** Where a capture's card goes: its (first) fish's page, or the frame's own
 *  page (its automatic result, and its review) for a capture with no measured
 *  fish. The frame link carries the card's place in the mission, so the frame
 *  page can offer the next frame. */
export function captureCardLink(slug: string, card: CaptureCard, mission: number): string {
  const [first] = card.fish;
  return first ? paths.fishDetail(slug, card.capture_id, first.fish_id) : frameLink(slug, mission, card);
}

/** A frame's page, with its place in the mission's order (`at`). */
export function frameLink(slug: string, mission: number, card: Pick<CaptureCard, "capture_id" | "captured_at">): string {
  return `${paths.frame(slug, mission, card.capture_id)}?at=${encodeURIComponent(captureCursor(card))}`;
}

/** The API's page cursor for a capture: its time and id (`observations_api`). */
export function captureCursor(card: Pick<CaptureCard, "capture_id" | "captured_at">): string {
  return `${card.captured_at}~${card.capture_id}`;
}

/** A cursor from a URL, if it is one: an ISO time and a uuid. */
export function parseCaptureCursor(value: string | undefined | null): string | null {
  if (!value) return null;
  const [at, id, ...rest] = value.split("~");
  if (rest.length || !at || !id) return null;
  if (Number.isNaN(Date.parse(at))) return null;
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) ? value : null;
}
