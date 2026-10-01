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

/** The web's names for the label kinds, as `liveProjectIds` takes them. */
export type LabelKind = "laser" | "species" | "headtail" | "dive-slate";

export type ActiveProjects = ProjectsByKind & {
  /** Projects Label Studio would not resolve, summed across kinds. Non-zero
   *  means the cards are an incomplete list of the outstanding work. */
  degraded: number;
  /** Kinds whose projects could not be asked for at all (v2: the API, or the
   *  Authentik token the web asks it with, did not answer). Their sections
   *  are empty because nothing is known, not because nothing is outstanding. */
  unavailable: LabelKind[];
};

// Fresh object per call — a shared constant would hand every caller the
// same mutable arrays.
const noActiveProjects = (): ActiveProjects => ({
  laser: [],
  species: [],
  headtail: [],
  slate: [],
  degraded: 0,
  unavailable: [],
});

/**
 * A kind's live project ids, or `null` when they could not be asked for.
 *
 * v2: v1 asked its API with a static Basic-auth password, so only the API
 * itself could fail this. v2 first mints the web's Authentik service token,
 * so an Authentik outage throws here too -- and the landing page is public,
 * its Results and Administration links need neither, so one unreachable
 * dependency must not 500 it. The failure is logged and reported per kind.
 */
async function askLiveProjectIds(kind: LabelKind, revalidate: number): Promise<number[] | null> {
  try {
    return await liveProjectIds(kind, revalidate);
  } catch (error) {
    console.error(`[active-projects] could not list ${kind} projects`, error);
    return null;
  }
}

export async function getActiveProjects(revalidate = 300): Promise<ActiveProjects> {
  // `liveProjectIds` owns the kill switch, the gate filter and the ordering —
  // the same definition triage uses. See `lib/label-projects.ts`.
  const kinds: LabelKind[] = ["laser", "species", "headtail", "dive-slate"];
  const [laserIds, speciesIds, headtailIds, slateIds] = await Promise.all(
    kinds.map((kind) => askLiveProjectIds(kind, revalidate)),
  );
  const unavailable = kinds.filter(
    (_, i) => [laserIds, speciesIds, headtailIds, slateIds][i] === null,
  );

  // Never resolve an empty list. With Label Studio switched off every list is
  // empty, and this is what keeps the page from touching it at all.
  const resolve = async (ids: number[] | null) =>
    ids === null || ids.length === 0
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
    unavailable,
  };
}
