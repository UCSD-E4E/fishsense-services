"use server";

import { revalidatePath } from "next/cache";
import { auth } from "@/auth";
import { isSlug, isUuid } from "@/lib/ids";
import { postReview, type PostResult, type ReviewBody } from "@/lib/reviews";

/**
 * Record a review of the frame as the signed-in member (0063). Server actions
 * are public endpoints: nothing here trusts the page. The API takes the
 * reviewer from the token, checks their membership and skill, and makes every
 * rule; this passes its answer back.
 */
export async function recordReviewAction(
  tenant: string,
  captureId: string,
  framePath: string,
  body: ReviewBody,
): Promise<PostResult> {
  const session = await auth();
  if (!session?.user || session.error || !session.accessToken) {
    return { ok: false, error: "Your session has ended: sign in again" };
  }
  if (!isSlug(tenant) || !isUuid(captureId)) return { ok: false, error: "No such frame" };
  const result = await postReview(session.accessToken, tenant, captureId, body);
  if (result.ok && framePath.startsWith(`/${tenant}/`)) revalidatePath(framePath);
  return result;
}
