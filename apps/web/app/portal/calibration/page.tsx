// Ported from fishsense-lite@77e8f8e5
// apps/fishsense-lite-web/app/portal/calibration/page.tsx.
// v2 change: the dives are read from the v2 API as the signed-in user.
import Link from "next/link";
import { getDives } from "@/lib/dives";
import { requirePortalUser } from "../guard";
import { CalibrationLinks } from "./calibration-links";

export const dynamic = "force-dynamic";

export default async function CalibrationPage() {
  const session = await requirePortalUser("/portal/calibration");

  let dives: Awaited<ReturnType<typeof getDives>> = [];
  let divesError: string | null = null;
  try {
    dives = await getDives(session.accessToken ?? "");
  } catch (error) {
    divesError = error instanceof Error ? error.message : "Failed to load dives";
  }

  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <header className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Dive calibration links
          </h1>
          <p className="mt-1 max-w-3xl text-sm text-slate-600 dark:text-slate-400">
            Link a dive that has no slate of its own to a sibling slate dive shot
            with the same camera and laser rig. The dive then borrows that
            dive&apos;s laser calibration, so it can be measured without a slate
            in-frame. A dive with its own slate self-calibrates and needs no link.
          </p>
        </div>
        <Link
          href="/portal"
          className="shrink-0 rounded-md border border-slate-300 bg-white px-3 py-1 text-sm font-medium shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
        >
          Portal
        </Link>
      </header>

      {divesError ? (
        <p className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-950 dark:text-red-300">
          Could not load dives: {divesError}
        </p>
      ) : (
        <CalibrationLinks dives={dives} />
      )}
    </main>
  );
}
