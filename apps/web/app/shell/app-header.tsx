import Link from "next/link";
import { Suspense } from "react";
import { auth, signOut } from "@/auth";
import { shellState } from "@/lib/shell";
import { Mark } from "./mark";
import { MainNav } from "./main-nav";
import { SignInFallback, SignInLink } from "./sign-in-link";

/**
 * The portal's header on every page (docs/design/README.md): the mark, the main
 * navigation, and the account. `shellState` decides who is signed in and who is
 * staff, and keeps an auth failure from taking the page down.
 */
export async function AppHeader() {
  const state = await shellState(() => auth());
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
        <MainNav staff={state.staff} />
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
