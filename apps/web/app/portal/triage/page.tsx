// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/portal/triage/page.tsx.
import Link from "next/link";
import { QUEUE_KINDS } from "@/lib/triage";
import { loadQueue } from "@/lib/triage-queue";
import { requirePortalUser } from "../guard";
import { TriageClient } from "./triage-client";

export const dynamic = "force-dynamic";

/**
 * The queue to open when `?kind=` is absent or is not a kind we serve.
 *
 * Unknown values fall back rather than 404ing: the parameter is a tab, and a
 * stale bookmark should land the labeler on a working queue.
 */
function resolveKind(raw: string | string[] | undefined) {
  const key = Array.isArray(raw) ? raw[0] : raw;
  return key && key in QUEUE_KINDS
    ? QUEUE_KINDS[key as keyof typeof QUEUE_KINDS]
    : QUEUE_KINDS.laser;
}

export default async function TriagePage({
  searchParams,
}: {
  searchParams: Promise<{ kind?: string | string[] }>;
}) {
  await requirePortalUser("/portal/triage");

  const kind = resolveKind((await searchParams).kind);

  let queue: Awaited<ReturnType<typeof loadQueue>> = {
    items: [],
    scanned: 0,
    projects: [],
    notWalked: 0,
  };
  let error: string | null = null;
  try {
    queue = await loadQueue(kind.key);
  } catch (e) {
    error = e instanceof Error ? e.message : "Could not load the queue";
  }

  return (
    // Inherits the root layout's background and theme like every other route.
    // It used to hardcode `bg-slate-950 text-slate-100` and `h-dvh`, which
    // painted over the layout's chrome and stayed dark in light mode — so the
    // page read as a different application.
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-4 px-4 py-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {kind.label} triage
          </h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            Accept the prediction, or skip it.
          </p>
        </div>
        <Link
          href="/portal"
          className="rounded-md border border-slate-300 bg-white px-3 py-1 text-sm font-medium shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
        >
          Portal
        </Link>
      </header>

      {/* Tabs, not a dropdown: two queues that are each a full-screen task,
          and the current one has to be obvious at a glance on a phone. */}
      <nav className="flex gap-2" aria-label="Triage queue">
        {Object.values(QUEUE_KINDS).map((option) => {
          const active = option.key === kind.key;
          return (
            <Link
              key={option.key}
              href={`/portal/triage?kind=${option.key}`}
              aria-current={active ? "page" : undefined}
              className={
                active
                  ? "rounded-md border border-slate-900 bg-slate-900 px-3 py-1 text-sm font-medium text-white dark:border-slate-100 dark:bg-slate-100 dark:text-slate-900"
                  : "rounded-md border border-slate-300 bg-white px-3 py-1 text-sm font-medium shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
              }
            >
              {option.label}
            </Link>
          );
        })}
      </nav>

      {error ? (
        <p className="rounded-md border border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-700/60 dark:bg-amber-950/40 dark:text-amber-200">
          {error}
        </p>
      ) : (
        <TriageClient
          items={queue.items}
          kindLabel={kind.label}
          scanned={queue.scanned}
          projects={queue.projects}
          notWalked={queue.notWalked}
        />
      )}
    </main>
  );
}
