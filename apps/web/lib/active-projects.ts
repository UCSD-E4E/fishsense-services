// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/active-projects.ts.
import { hasOutstandingTasks, isPublished, liveProjectIds } from "./label-projects";
import { getProjects, type LabelStudioProject } from "./label-studio";

/** The four labeling kinds. Separate from `ActiveProjects` so `buildSections`
 *  can iterate the kinds without `degraded` being a possible key. */
export type ProjectsByKind = {
  laser: LabelStudioProject[];
  species: LabelStudioProject[];
  headtail: LabelStudioProject[];
  slate: LabelStudioProject[];
};

export type ActiveProjects = ProjectsByKind & {
  /** Projects Label Studio would not resolve, summed across kinds. Non-zero
   *  means the cards are an incomplete list of the outstanding work. */
  degraded: number;
};

// Fresh object per call — a shared constant would hand every caller the
// same mutable arrays.
const noActiveProjects = (): ActiveProjects => ({
  laser: [],
  species: [],
  headtail: [],
  slate: [],
  degraded: 0,
});

export async function getActiveProjects(revalidate = 300): Promise<ActiveProjects> {
  // `liveProjectIds` owns the kill switch, the gate filter and the ordering —
  // the same definition triage uses. See `lib/label-projects.ts`.
  const [laserIds, speciesIds, headtailIds, slateIds] = await Promise.all([
    liveProjectIds("laser", revalidate),
    liveProjectIds("species", revalidate),
    liveProjectIds("headtail", revalidate),
    liveProjectIds("dive-slate", revalidate),
  ]);

  // Never resolve an empty list. With Label Studio switched off every list is
  // empty, and this is what keeps the page from touching it at all.
  const resolve = async (ids: number[]) =>
    ids.length === 0
      ? { projects: [] as LabelStudioProject[], degraded: 0 }
      : getProjects(ids, revalidate);

  const [laser, species, headtail, slate] = await Promise.all([
    resolve(laserIds),
    resolve(speciesIds),
    resolve(headtailIds),
    resolve(slateIds),
  ]);

  // Two narrowings, both from Label Studio's own answer about the project:
  // drafts are not ready for a labeler, and a fully-labeled project has
  // nothing left for one. The second is what keeps a card from outliving the
  // work by up to a sync cycle — see `hasOutstandingTasks`.
  const live = (resolved: LabelStudioProject[]) =>
    resolved.filter(isPublished).filter(hasOutstandingTasks);

  return {
    laser: live(laser.projects),
    species: live(species.projects),
    headtail: live(headtail.projects),
    slate: live(slate.projects),
    degraded:
      laser.degraded + species.degraded + headtail.degraded + slate.degraded,
  };
}
