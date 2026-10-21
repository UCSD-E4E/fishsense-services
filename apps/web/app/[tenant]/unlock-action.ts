"use server";

import { revalidatePath } from "next/cache";
import { auth } from "@/auth";
import { accessTokenIsFresh } from "@/lib/authz";
import { isTenantSlug, paths } from "@/lib/tenant-paths";
import { unlockTenant } from "@/lib/system-admin";

/**
 * A system admin's deliberate unlock of a tenant they are not in (0056): they
 * become its admin for a while, and the API records it. A server action is a
 * public endpoint, so it checks the session itself; the API decides whether
 * the caller may unlock at all.
 */
export async function unlockTenantAction(slug: string, confirmed: boolean): Promise<string> {
  if (!confirmed) throw new Error("Confirm that you mean to change this tenant's data");
  if (!isTenantSlug(slug)) throw new Error("Not a tenant");
  const session = await auth();
  if (!session?.accessToken || !accessTokenIsFresh(session)) {
    throw new Error("Sign in again to unlock");
  }
  const until = await unlockTenant(session.accessToken, slug);
  revalidatePath(paths.tenant(slug), "layout");
  return until;
}
