/**
 * Reviewing the automatic results on a frame (the owner, 2026-10-21: "I was
 * hoping to use this app to review the auto process"; the API is 0063).
 *
 * A member reviews one kind of the frame's result: the laser dot, the head and
 * tail, or the species. They accept it, fix it, or reject it with a reason.
 * Their skill in that kind decides whether it counts:
 * - a Trusted or Expert review counts at once;
 * - a Learning review waits for a Trusted or Expert confirm, or for a second
 *   learner who agrees.
 * The API keeps every review and makes the rules. This module asks it, and
 * works out what the page offers: where each kind stands, and who may back
 * which waiting review.
 */
import { apiClient, detailOf, failure, type Schemas } from "./api/client";
import type { AutomaticFrame } from "./automatic";
import { safeSlug, safeUuid } from "./ids";

export type Review = Schemas["ReviewView"];
export type ReviewBody = Schemas["ReviewBody"];
export type Recorded = Schemas["RecordedReviewView"];
export type Kind = ReviewBody["kind"];
export type Level = Review["level"];
export type Reason = NonNullable<ReviewBody["reason"]>;
export type Skills = Record<Kind, Level>;

export const KINDS: { kind: Kind; label: string }[] = [
  { kind: "laser", label: "Laser dot" },
  { kind: "head_tail", label: "Head and tail" },
  { kind: "species", label: "Species" },
];

export const LEVEL_LABEL: Record<Level, string> = {
  learning: "Learning",
  trusted: "Trusted",
  expert: "Expert",
};

/** Every review of the frame, oldest first. */
export async function getCaptureReviews(
  accessToken: string,
  slug: string,
  captureId: string,
): Promise<Review[]> {
  const capture_id = safeUuid(captureId, "capture");
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/captures/{capture_id}/reviews",
    { params: { path: { slug: safeSlug(slug), capture_id } } },
  );
  if (data !== undefined) return data;
  throw failure("fishsense-api reviews failed", response, error);
}

/** The caller's level in each kind. */
export async function getMySkills(accessToken: string, slug: string): Promise<Skills> {
  const { data, error, response } = await apiClient(accessToken).GET("/tenants/{slug}/me/skills", {
    params: { path: { slug: safeSlug(slug) } },
  });
  if (data !== undefined) return data as Skills;
  throw failure("fishsense-api skills failed", response, error);
}

export type PostResult = { ok: true; recorded: Recorded } | { ok: false; error: string };

/** Record a review as the caller. A refusal comes back as the API's reason:
 *  it is written for the reviewer ("that render was made under an older
 *  camera calibration..."). */
export async function postReview(
  accessToken: string,
  slug: string,
  captureId: string,
  body: ReviewBody,
): Promise<PostResult> {
  const capture_id = safeUuid(captureId, "capture");
  const { data, error, response } = await apiClient(accessToken).POST(
    "/tenants/{slug}/captures/{capture_id}/reviews",
    { params: { path: { slug: safeSlug(slug), capture_id } }, body },
  );
  if (data !== undefined) return { ok: true, recorded: data };
  return {
    ok: false,
    error: detailOf(error) ?? `The review wasn't recorded (${response.status} ${response.statusText})`,
  };
}

/**
 * Where a kind stands on the frame:
 * - `decided`: the review whose word counts now. A confirm, or a learner's
 *   agreement, decides for the review it backs;
 * - `waiting`: the Learning reviews made since then that don't count yet.
 */
export function kindState(reviews: Review[], kind: Kind): { decided: Review | null; waiting: Review[] } {
  const mine = reviews.filter((r) => r.kind === kind);
  const last = mine.findLastIndex((r) => r.counts);
  if (last < 0) return { decided: null, waiting: mine.filter((r) => !r.counts) };
  const counted = mine[last];
  const decided = counted.confirms_review_id
    ? (mine.find((r) => r.review_id === counted.confirms_review_id) ?? counted)
    : counted;
  return { decided, waiting: mine.slice(last + 1).filter((r) => !r.counts) };
}

/** A Trusted or Expert reviewer may confirm a waiting review, never their own. */
export function canConfirm(level: Level, review: Review): boolean {
  return level !== "learning" && !review.mine;
}

/** The waiting review a learner's own review names: the latest by someone
 *  else. The API counts both if they agree, and keeps the learner's as one
 *  more waiting review if not. */
export function agreeTarget(level: Level, waiting: Review[]): string | null {
  if (level !== "learning") return null;
  return waiting.findLast((r) => !r.mine)?.review_id ?? null;
}

/** An accept: the model's own points, exactly, on the render they're on.
 *  Null when the frame has nothing of that kind to accept. */
export function acceptBody(kind: Kind, frame: AutomaticFrame): ReviewBody | null {
  const base = { kind, action: "accept" as const, automatic_head_tail_prediction_id: frame.prediction_id };
  if (kind === "species") {
    return frame.species ? { ...base, scientific_name: frame.species } : null;
  }
  if (!frame.render_id) return null;
  if (kind === "laser") {
    return frame.laser ? { ...base, render_id: frame.render_id, point: frame.laser } : null;
  }
  return frame.head && frame.tail
    ? { ...base, render_id: frame.render_id, head: frame.head, tail: frame.tail }
    : null;
}

const REASON: Record<Reason, string> = {
  dot_wrong: "The dot is wrong",
  bad_mask: "Bad mask",
  wrong_fish: "Wrong fish",
  fish_unmeasurable: "Fish can't be measured",
  no_fish: "No fish",
};

/** The reject reasons that fit a kind, most specific first. */
export function reasonsFor(kind: Kind): { value: Reason; label: string }[] {
  const values: Reason[] =
    kind === "laser"
      ? ["dot_wrong", "no_fish"]
      : kind === "head_tail"
        ? ["bad_mask", "wrong_fish", "fish_unmeasurable", "no_fish"]
        : ["wrong_fish", "no_fish"];
  return values.map((value) => ({ value, label: REASON[value] }));
}

export function reasonLabel(reason: string | null): string {
  return reason && reason in REASON ? REASON[reason as Reason] : (reason ?? "");
}
