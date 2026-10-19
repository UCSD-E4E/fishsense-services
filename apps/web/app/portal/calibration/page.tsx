// Ported from fishsense-lite@77e8f8e5
// apps/fishsense-lite-web/app/portal/calibration/page.tsx.
// v2 change: the missions are read from the v2 API as the signed-in user.
import Link from "next/link";
import { getMissions } from "@/lib/missions";
import { requirePortalUser } from "../guard";
import { CalibrationLinks } from "./calibration-links";

export const dynamic = "force-dynamic";

export default async function CalibrationPage() {
  const session = await requirePortalUser("/portal/calibration");

  let missions: Awaited<ReturnType<typeof getMissions>> = [];
  let missionsError: string | null = null;
  try {
    missions = await getMissions(session.accessToken ?? "");
  } catch (error) {
    missionsError = error instanceof Error ? error.message : "Failed to load missions";
  }

  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <header className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Mission calibration links
          </h1>
          <p className="mt-1 max-w-3xl text-sm text-slate-600 dark:text-slate-400">
            Link a mission that has no slate of its own to a sibling slate mission shot
            with the same camera and laser rig. The mission then borrows that
            mission&apos;s laser calibration, so it can be measured without a slate
            in-frame. A mission with its own slate self-calibrates and needs no link.
          </p>
        </div>
        <Link
          href="/portal"
          className="shrink-0 rounded-md border border-slate-300 bg-white px-3 py-1 text-sm font-medium shadow-xs transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
        >
          Portal
        </Link>
      </header>

      {missionsError ? (
        <p className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-950 dark:text-red-300">
          Could not load missions: {missionsError}
        </p>
      ) : (
        <CalibrationLinks missions={missions} />
      )}
    </main>
  );
}
