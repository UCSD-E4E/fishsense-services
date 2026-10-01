// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/label-projects.ts.
import { labelStudioEnabled } from "./env";
import { getProjectIds, type LabelKind } from "./fishsense-api";
import type { LabelStudioProject } from "./label-studio";

/**
 * Which Label Studio projects a human should be sent to, for one label kind.
 *
 * **One definition, two consumers.** The landing page renders these as cards
 * and triage walks their tasks; before this existed each answered the question
 * for itself and they drifted. The gate filter reached triage by inheritance
 * rather than by anyone deciding it should, and the Label Studio kill switch
 * reached it not at all — turning `LABEL_STUDIO_ENABLED` off blanked the
 * landing page while triage carried on calling Label Studio.
 *
 * Returns ids rather than resolved projects on purpose: the two consumers need
 * genuinely different fetch strategies — the landing page resolves every
 * project to title them, triage resolves them lazily and stops as soon as it
 * has a batch — and that difference is legitimate. What is not legitimate is
 * each one re-deciding *which* projects count.
 *
 * Order is NOT part of the policy: the landing page shows cards in the order
 * the API returns, and reordering them here would change that surface for a
 * reason belonging to the other one. Triage sorts for itself.
 */
export async function liveProjectIds(
  kind: LabelKind,
  revalidate: number,
): Promise<number[]> {
  // Nothing to send anyone to when the integration is switched off. This is
  // the check triage never had.
  if (!labelStudioEnabled()) return [];
  return getProjectIds(kind, revalidate);
}

/**
 * Drop unpublished projects.
 *
 * The ids come from fishsense-api, which is derived from label rows and knows
 * nothing about Label Studio's publish state — so a draft still being
 * populated, or one deliberately held back, would otherwise be offered.
 *
 * `!== false` rather than truthiness: only an explicit unpublished flag hides
 * a project, so a Label Studio response change cannot silently blank
 * everything.
 */
export function isPublished(project: LabelStudioProject): boolean {
  return project.isPublished !== false;
}

/**
 * Drop projects Label Studio itself reports as fully labeled.
 *
 * The ids come from fishsense-api, which answers "outstanding" from our own
 * `<kind>Label.completed` column — and the ONLY writer of that column is the
 * hourly Label Studio sync (`sync_<kind>_labels_for_label_studio_project_activity`,
 * `label.completed = task.is_labeled`). So between a labeler finishing a
 * project and the next sync run, the api still calls it outstanding and the
 * card stays up. Measured in prod on 2026-09-08: dive 516's species project
 * (285759) took its last annotation at 05:47 UTC and was still listed until
 * the sync just after 06:00.
 *
 * Syncing more often would narrow that window without closing it. Both
 * surfaces already fetch every project from Label Studio to resolve its title,
 * and that response carries the counts, so this asks the authority instead of
 * a copy of its answer. What remains is each caller's own cache — the landing
 * page reads it through a 300s `revalidate`, so a finished project can still
 * show for up to five minutes. An hour, bounded by a sync we do not control,
 * becomes five minutes bounded by one we do.
 *
 * This does NOT make the api's list redundant: it is what decides which
 * projects are worth asking about at all (and, for laser, applies the
 * auto-accept gate filter). Label Studio only ever narrows it.
 *
 * Fails open in four ways, all deliberate. Only a coherent "everything here
 * is labeled" hides anything:
 *
 * * Either count missing -> kept. An absent field means we did not learn the
 *   answer, not that the work is done; the same reasoning as `isPublished`.
 * * A non-numeric count -> kept, for the same reason.
 * * More finished than the project holds -> kept. That pair is incoherent, so
 *   it is an unusable answer rather than an emphatic one, and burying real
 *   labeling work on the strength of a reply we know is wrong is the one
 *   outcome this filter must not produce.
 * * Zero tasks -> kept. Vacuous truth reads as "not complete", the convention
 *   `dive_pipeline_status`'s `*_labeling_complete` flags already use, and a
 *   project with no tasks is a populate that has not happened rather than a
 *   labeling job that is finished.
 */
export function hasOutstandingTasks(project: LabelStudioProject): boolean {
  const total = project.taskCount;
  const finished = project.finishedTaskCount;
  if (!isCount(total) || !isCount(finished)) return true;
  if (total <= 0) return true;
  if (finished > total) return true;
  return finished < total;
}

/** A usable count: present, a number, and not NaN/Infinity. */
function isCount(value: number | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
