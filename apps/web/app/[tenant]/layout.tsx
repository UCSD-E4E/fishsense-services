import { notFound, unstable_rethrow } from "next/navigation";
import { auth } from "@/auth";
import { tenantFor } from "@/app/_shell/tenant";
import { shellState } from "@/lib/shell";
import { banner, type Banner } from "@/lib/system-admin";
import { isTenantSlug } from "@/lib/tenant-paths";
import { SystemAdminBanner } from "./system-admin-banner";

type Props = { children: React.ReactNode; params: Promise<{ tenant: string }> };

/**
 * Every tenant page sits under its slug (`/lab/fish`). Anything else in that
 * place -- a file a browser asks for (`/favicon.ico`), a reserved word -- is a
 * 404 here, before any page signs the visitor in or redirects them.
 *
 * A system admin in a tenant they are not a member of sees, on every page, how
 * they are there: read-only with a deliberate unlock, or unlocked until when
 * (0056). The pages sign everyone else in, or 404.
 */
export default async function TenantLayout({ children, params }: Props) {
  const { tenant } = await params;
  if (!isTenantSlug(tenant)) notFound();

  let shown: { name: string; banner: Banner } | null = null;
  const state = await shellState(() => auth());
  if (state.accessToken) {
    try {
      const found = await tenantFor(state.accessToken, tenant);
      const b = banner(found);
      if (b) shown = { name: found.name, banner: b };
    } catch (error) {
      // A 404 is the page's to give; anything else costs the banner, not the page.
      unstable_rethrow(error);
      console.error("tenant layout: reading the caller's access failed", error);
    }
  }
  return (
    <>
      {shown && <SystemAdminBanner slug={tenant} name={shown.name} banner={shown.banner} />}
      {children}
    </>
  );
}
