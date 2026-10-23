import { cookies } from "next/headers";
import { redirect, unstable_rethrow } from "next/navigation";
import type { Session } from "next-auth";
import { auth } from "@/auth";
import { homeStep } from "@/lib/home";
import { LAST_TENANT_COOKIE, paths } from "@/lib/tenant-paths";
import { myTenants } from "./_shell/tenant";
import { PublicHome } from "./_shell/public-home";

export const dynamic = "force-dynamic";

/**
 * Signed in, home is the tenant's Today (`/lab`): the one used last, if the
 * user is still in it, else their first -- with a fresh token: an expired one
 * is renewed first (`homeStep`). Signed out, it is the public page: what
 * FishSense does, and what opted-in tenants have measured, by region.
 */
export default async function HomePage() {
  let session: Session | null = null;
  try {
    session = await auth();
  } catch (error) {
    // As the shell does (lib/shell.ts): an auth failure renders signed out.
    unstable_rethrow(error);
    console.error("home: reading the session failed; rendering signed out", error);
  }
  const step = homeStep(session);
  if (step.kind === "redirect") redirect(step.to);
  if (step.kind === "tenant") {
    let slug: string | undefined;
    try {
      const tenants = await myTenants(step.accessToken);
      const last = (await cookies()).get(LAST_TENANT_COOKIE)?.value;
      slug = (tenants.find((t) => t.slug === last) ?? tenants[0])?.slug;
    } catch (error) {
      unstable_rethrow(error);
      console.error("home: reading the user's tenants failed", error);
    }
    if (slug) redirect(paths.tenant(slug));
  }
  return <PublicHome signedIn={step.kind === "tenant"} />;
}
