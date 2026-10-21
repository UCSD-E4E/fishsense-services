import Link from "next/link";
import { cookies } from "next/headers";
import { unstable_rethrow } from "next/navigation";
import { Suspense } from "react";
import { auth, signOut } from "@/auth";
import { shellState } from "@/lib/shell";
import { LAST_TENANT_COOKIE } from "@/lib/tenant-paths";
import { Mark } from "./mark";
import { MainNav } from "./main-nav";
import { SignInFallback, SignInLink } from "./sign-in-link";
import { myTenants } from "./tenant";
import { TenantSwitcher, type SwitcherTenant } from "./tenant-switcher";

/** The user's tenants for the switcher; none when they can't be read, so a
 *  failing API costs the switcher, not the page. */
async function switcherTenants(accessToken: string | null): Promise<SwitcherTenant[]> {
  if (!accessToken) return [];
  try {
    const tenants = await myTenants(accessToken);
    return tenants.map((t) => ({ slug: t.slug, name: t.name, role: t.role }));
  } catch (error) {
    unstable_rethrow(error);
    console.error("app shell: reading the user's tenants failed", error);
    return [];
  }
}

/**
 * The portal's header on every page (docs/design/README.md): the mark, the main
 * navigation, and the account. `shellState` decides who is signed in and who is
 * staff, and keeps an auth failure from taking the page down.
 */
export async function AppHeader() {
  const state = await shellState(() => auth());
  const tenants = await switcherTenants(state.accessToken);
  // The cookie is only a preference: it counts while the user is still in it
  // (the middleware remembers any tenant-shaped path, a 404 included), and
  // otherwise their first tenant stands in.
  const cookie = (await cookies()).get(LAST_TENANT_COOKIE)?.value;
  const lastTenant =
    tenants.find((t) => t.slug === cookie)?.slug ?? tenants[0]?.slug ?? null;
  return (
    <header className="border-b border-line bg-surface">
      <nav
        aria-label="Main"
        className="mx-auto flex min-h-16 max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-6"
      >
        <Link href="/" className="flex items-center gap-2.5 font-semibold text-fg">
          <Mark />
          FishSense
        </Link>
        {/* Signed out there is nothing to navigate: every section needs a tenant. */}
        {state.signedIn && (
          <>
            <TenantSwitcher tenants={tenants} lastTenant={lastTenant} />
            <MainNav staff={state.staff} lastTenant={lastTenant} />
          </>
        )}
        <div className="ml-auto flex items-center gap-3">
          {state.signedIn ? (
            <>
              <span className="text-sm text-fg-2">{state.name}</span>
              <form
                action={async () => {
                  "use server";
                  await signOut({ redirectTo: "/" });
                }}
              >
                <button
                  type="submit"
                  className="inline-flex h-10 items-center rounded-control border border-line px-3 text-sm text-fg hover:bg-raised"
                >
                  Sign out
                </button>
              </form>
            </>
          ) : (
            <Suspense fallback={<SignInFallback />}>
              <SignInLink />
            </Suspense>
          )}
        </div>
      </nav>
    </header>
  );
}
