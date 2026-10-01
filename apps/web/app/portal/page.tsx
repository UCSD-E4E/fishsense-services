import Link from "next/link";
import { redirect } from "next/navigation";
import { auth, signOut } from "@/auth";
import { accessTokenIsFresh, explainDenial, portalAccess } from "@/lib/authz";
import { tenantSlug } from "@/lib/env";
import { PORTAL_SECTIONS } from "@/lib/portal-sections";
import { refreshPath } from "@/lib/session-refresh";

export const dynamic = "force-dynamic";

/**
 * The portal index, which routes and does nothing else.
 *
 * Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/portal/page.tsx.
 *
 * It keeps the unauthorized dead end, which is not incidental: every other
 * portal page redirects here when the check fails, so this is where that has
 * to be explained.
 *
 * v2 change: the check is the user's admin role in the tenant, as the v2 API
 * reports it (lib/authz.ts), not an Authentik group; an expired access token
 * is renewed first, which re-reads that role.
 */
export default async function PortalPage() {
  const session = await auth();
  if (!session?.user) {
    redirect(`/api/auth/signin?callbackUrl=${encodeURIComponent("/portal")}`);
  }
  if (!session.error && !accessTokenIsFresh(session)) {
    redirect(refreshPath("/portal"));
  }
  const user = session.user;
  const access = portalAccess(session);

  // Authenticated is not authorized: signing in only proves an account in
  // the Authentik realm. Render a dead end rather than redirecting to
  // sign-in, which would loop forever for a user who is already signed in
  // and simply lacks the role.
  if (!access.ok) {
    return (
      <main className="mx-auto max-w-2xl px-6 py-12">
        <h1 className="text-2xl font-semibold tracking-tight">Portal</h1>
        <p className="mt-4 text-sm text-slate-600 dark:text-slate-400">
          Signed in as {user?.email ?? "unknown"}, but this account may not use
          the portal.
        </p>
        <p className="mt-2 text-sm text-slate-500">
          {explainDenial(access.reason, tenantSlug())}
        </p>
        <form
          action={async () => {
            "use server";
            await signOut({ redirectTo: "/portal" });
          }}
        >
          <button
            type="submit"
            className="mt-6 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
          >
            Sign out and retry
          </button>
        </form>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <header className="mb-8 flex items-start justify-between gap-4">
        <h1 className="text-3xl font-semibold tracking-tight">Portal</h1>
        <div className="flex items-start gap-4">
          <div className="text-right text-sm leading-tight">
            <div className="font-medium">{user?.name ?? "—"}</div>
            <div className="text-slate-500">{user?.email ?? "—"}</div>
            <div className="text-xs text-slate-400">
              {session.role} · {tenantSlug()}
            </div>
            <div className="text-xs text-slate-400">
              {user?.groups?.length ? user.groups.join(", ") : "no groups"}
            </div>
          </div>
          <form
            action={async () => {
              "use server";
              await signOut({ redirectTo: "/" });
            }}
          >
            <button
              type="submit"
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
            >
              Sign out
            </button>
          </form>
        </div>
      </header>

      <ul className="grid gap-4 sm:grid-cols-2">
        {PORTAL_SECTIONS.map((section) => (
          <li key={section.href}>
            {/* The whole card is the target, not a "read more" buried in it —
                this is used one-handed on a phone. */}
            <Link
              href={section.href}
              className="block h-full rounded-lg border border-slate-200 bg-white p-6 shadow-sm transition hover:border-slate-300 hover:shadow-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 dark:border-slate-800 dark:bg-slate-900 dark:hover:border-slate-700"
            >
              <h2 className="text-lg font-medium">
                {section.title}
                <span aria-hidden className="ml-1.5 text-slate-400">
                  &rarr;
                </span>
              </h2>
              <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
                {section.description}
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
