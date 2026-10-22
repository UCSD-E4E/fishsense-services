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
): Promise<CaptureCardPage | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/missions/{number}/captures",
    { params: { path: { slug: safeSlug(slug), number }, query: after ? { after } : {} } },
  );
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api mission captures failed", response, error);
}

/** Where a capture's card goes: its (first) fish's page, or the frame's own
 *  page (its automatic result) for a capture with no measured fish. */
export function captureCardLink(slug: string, card: CaptureCard, mission: number): string {
  const [first] = card.fish;
  return first
    ? paths.fishDetail(slug, card.capture_id, first.fish_id)
    : paths.frame(slug, mission, card.capture_id);
}
