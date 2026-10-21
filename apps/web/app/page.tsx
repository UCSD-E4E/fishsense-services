import { cookies } from "next/headers";
import { redirect, unstable_rethrow } from "next/navigation";
import { auth } from "@/auth";
import { shellState } from "@/lib/shell";
import { LAST_TENANT_COOKIE, paths } from "@/lib/tenant-paths";
import { myTenants } from "./_shell/tenant";
import { PublicHome } from "./_shell/public-home";

export const dynamic = "force-dynamic";

/**
 * Signed in, home is the tenant's Today (`/lab`): the one used last, if the
 * user is still in it, else their first. Signed out, it is the public page:
 * what FishSense does, and what opted-in tenants have measured, by region.
 */
export default async function HomePage() {
  const state = await shellState(() => auth());
  if (state.accessToken) {
    let slug: string | undefined;
    try {
      const tenants = await myTenants(state.accessToken);
      const last = (await cookies()).get(LAST_TENANT_COOKIE)?.value;
      slug = (tenants.find((t) => t.slug === last) ?? tenants[0])?.slug;
    } catch (error) {
      unstable_rethrow(error);
      console.error("home: reading the user's tenants failed", error);
    }
    if (slug) redirect(paths.tenant(slug));
  }
  return <PublicHome />;
}
