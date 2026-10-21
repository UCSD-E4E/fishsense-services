import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { LAST_TENANT_COOKIE, legacyRedirect } from "@/lib/tenant-paths";
import { requireSignedIn } from "./guard";
import { myTenants } from "./tenant";

/**
 * An old index page that names no tenant (`/fish`, `/missions?purpose=…`):
 * sign in, then go to the same page in the tenant used last, if the user is
 * still in it, else their first. The middleware moved every old link that
 * did name one.
 */
export async function PickTenant({
  path,
  search,
}: {
  path: "/fish" | "/deployments" | "/missions" | "/captures";
  search: Record<string, string | string[] | undefined>;
}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(search)) {
    for (const v of Array.isArray(value) ? value : value === undefined ? [] : [value]) {
      query.append(key, v);
    }
  }
  const session = await requireSignedIn(`${path}${query.size ? `?${query}` : ""}`);
  const tenants = await myTenants(session.accessToken ?? "");
  const last = (await cookies()).get(LAST_TENANT_COOKIE)?.value;
  const pick = tenants.find((t) => t.slug === last) ?? tenants[0];
  const target = pick ? legacyRedirect(path, query, pick.slug) : null;
  if (target) redirect(target);
  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <h1 className="text-2xl font-semibold">No tenant yet</h1>
      <p className="mt-2 text-fg-2">
        This account isn&apos;t a member of any tenant yet. Ask a tenant&apos;s admin to add you.
      </p>
    </main>
  );
}
