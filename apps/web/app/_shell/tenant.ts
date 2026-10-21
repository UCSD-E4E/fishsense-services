import { notFound } from "next/navigation";
import { cache } from "react";
import { getMyTenants, type TenantMembership } from "@/lib/fishsense-api";

/**
 * The caller's tenants, asked once per request: the shell's switcher and the
 * page both need them.
 */
export const myTenants = cache((accessToken: string) => getMyTenants(accessToken));

/**
 * The tenant a page's path names (`/lab/fish`). A tenant the caller is not in
 * is a 404, the same answer as one that does not exist, so the path says
 * nothing about other tenants. Call it outside a `try`, or rethrow with
 * `unstable_rethrow`: `notFound()` works by throwing.
 */
export function memberOf(tenants: TenantMembership[], slug: string): TenantMembership {
  const tenant = tenants.find((t) => t.slug === slug);
  if (!tenant) notFound();
  return tenant;
}
