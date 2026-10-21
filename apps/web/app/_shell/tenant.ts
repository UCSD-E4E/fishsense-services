import { notFound } from "next/navigation";
import { cache } from "react";
import { getMyTenants, type TenantMembership } from "@/lib/fishsense-api";
import { getAllTenants, getTenantAccess } from "@/lib/system-admin";

/**
 * The caller's tenants, asked once per request: the shell's switcher and the
 * page both need them.
 */
export const myTenants = cache((accessToken: string) => getMyTenants(accessToken));
const tenantAccess = cache((accessToken: string, slug: string) =>
  getTenantAccess(accessToken, slug),
);
export const allTenants = cache((accessToken: string) => getAllTenants(accessToken));

export type PageTenant = TenantMembership & {
  /** `member`, or how a system admin is here (0056). */
  access: string;
  unlockedUntil: string | null;
};

/**
 * The tenant a page's path names (`/lab/fish`): the caller's membership, else
 * a system admin's access to it -- read-only, or its admin while an unlock
 * lasts (0056; the API records the visit). Anyone else gets a 404, the same
 * answer as for a tenant that does not exist, so the path says nothing about
 * other tenants. Call it outside a `try`, or rethrow with `unstable_rethrow`:
 * `notFound()` works by throwing.
 */
export async function tenantFor(accessToken: string, slug: string): Promise<PageTenant> {
  const mine = (await myTenants(accessToken)).find((t) => t.slug === slug);
  if (mine) return { ...mine, access: "member", unlockedUntil: null };
  const access = await tenantAccess(accessToken, slug);
  if (!access) notFound();
  const name = (await allTenants(accessToken)).find((t) => t.slug === slug)?.name ?? slug;
  return {
    slug,
    name,
    role: access.role,
    isAdmin: access.isAdmin,
    access: access.access,
    unlockedUntil: access.unlockedUntil,
  };
}
