"use server";

import { revalidatePath } from "next/cache";
import { auth, unstable_update } from "@/auth";
import { accessTokenIsFresh } from "@/lib/authz";
import { safeSlug } from "@/lib/ids";
import { setMemberSkill, type Kind, type Level } from "@/lib/members";
import { paths } from "@/lib/tenant-paths";

/**
 * An admin sets a member's review level in one kind (0063). Server actions
 * are public endpoints: this re-checks the sign-in and passes the API's
 * answer back. The API checks the caller is the tenant's admin, and the
 * person its member.
 */
export async function setSkillAction(
  slug: string,
  userId: string,
  kind: Kind,
  level: Level,
): Promise<{ ok: true } | { ok: false; error: string }> {
  let session = await auth();
  if (!session?.user) return { ok: false, error: "Not signed in" };
  if (!accessTokenIsFresh(session)) {
    session = await unstable_update({});
    if (!session || session.error || !accessTokenIsFresh(session)) {
      return { ok: false, error: "Your sign-in has expired. Reload the page to sign in again." };
    }
  }
  try {
    const result = await setMemberSkill(session.accessToken!, safeSlug(slug), userId, kind, level);
    if (result.ok) revalidatePath(paths.members(slug));
    return result;
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Not saved" };
  }
}
