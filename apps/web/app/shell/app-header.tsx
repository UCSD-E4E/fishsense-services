import Link from "next/link";
import { auth } from "@/auth";
import { Mark } from "./mark";
import { MainNav } from "./main-nav";

/**
 * The portal's header on every page (docs/design/README.md): the mark, the main
 * navigation, and sign-in. Labelling shows to the tenant's admins, who are the
 * portal's staff (`portalAccess`).
 */
export async function AppHeader() {
  const session = await auth();
  const signedIn = Boolean(session?.user);
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
        <MainNav staff={session?.isAdmin === true} />
        <div className="ml-auto">
          {signedIn ? (
            <span className="text-sm text-fg-2">{session?.user?.name ?? session?.user?.email}</span>
          ) : (
            <Link
              href="/api/auth/signin?callbackUrl=%2F"
              prefetch={false}
              className="inline-flex h-10 items-center rounded-control bg-teal px-4 text-sm font-semibold text-on-teal"
            >
              Sign in
            </Link>
          )}
        </div>
      </nav>
    </header>
  );
}
