// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/triage-queue.ts.
import { hasOutstandingTasks, isPublished, liveProjectIds } from "./label-projects";
import { getProject } from "./label-studio";
import { getTask, listTasks } from "./label-studio-tasks";
import {
  QUEUE_KINDS,
  diveNameFromTitle,
  isTriageable,
  keypointsOf,
  pickPrediction,
  rejectionReason,
  type Keypoint,
  type LsRegion,
  type QueueKind,
} from "./triage";

export type TriageItem = {
  taskId: number;
  projectId: number;
  diveName: string;
  keypoints: Keypoint[];
  /** Passed back verbatim on accept — never rebuilt. */
  result: LsRegion[];
  partial: boolean;
};

/**
 * What one project contributed, and why.
 *
 * Per project rather than a flat sample. The first version capped the whole
 * scan at five reasons, so the first project consumed every slot and every
 * later project's outcome was invisible — which made a project that WAS walked
 * look like one that was never offered.
 */
export type ProjectOutcome = {
  projectId: number;
  title?: string;
  /** Tasks the project returned on the first page. */
  tasks: number;
  /** Triageable items taken from it. */
  taken: number;
  /** Refusal reason -> how many tasks it applied to. */
  reasons: Record<string, number>;
  /** Set when the project itself could not be read. */
  error?: string;
  /** The hydration budget ran out before this project was exhausted. */
  truncated?: boolean;
};

export type QueueReport = {
  items: TriageItem[];
  scanned: number;
  projects: ProjectOutcome[];
  /** Projects discovery offered but the walk did not reach. */
  notWalked: number;
};

/** Strips task ids so reasons group: "task 41: is_labeled" -> "is_labeled". */
export function reasonKey(reason: string): string {
  return reason.replace(/^task \d+: /, "");
}

/**
 * How many task-detail fetches one load may spend.
 *
 * `/api/tasks/?project=N` returns task rows with NO `predictions` and NO
 * `annotations` key — verified against Label Studio 1.13.1:
 *
 *     list   -> { id, is_labeled, data, ... }        no predictions key at all
 *     detail -> { id, is_labeled, data, predictions[], annotations[] }
 *
 * So a candidate must be hydrated from the detail endpoint before it can be
 * judged, and each hydration is a request. Bounded, because a project can hold
 * dozens of tasks that all turn out unusable and one load should not spend a
 * request on every one; when the budget runs out the project is reported as
 * truncated rather than as exhausted.
 */
const MAX_HYDRATIONS_PER_LOAD = 30;

/** How many projects to WALK — page the task list of — before giving up on
 *  filling a batch. */
const MAX_PROJECTS_WALKED_PER_LOAD = 12;

/** How many projects to RESOLVE — one `getProject` each — while looking for
 *  those twelve. Unpublished and finished projects cost a resolution and no
 *  walk, so this is the bound that keeps a page load from fanning out over
 *  Label Studio when a long run of them sits at the front of the list. */
const MAX_PROJECTS_RESOLVED_PER_LOAD = 24;

/**
 * The next batch of triageable tasks for a kind, newest dive first.
 *
 * Discovery goes through fishsense-api rather than Label Studio: one request
 * names the projects that still hold outstanding label rows, which is
 * authoritative where anything derived from Label Studio is inferred. When the
 * API cannot answer we keep every project — treating "cannot ask" as "nothing
 * to do" would report an empty queue while Label Studio is full of work, which
 * is the same silent-loss shape as writing on skip.
 */
export async function loadQueue(
  kindKey: QueueKind["key"],
  want = 12,
  revalidate = 0,
): Promise<QueueReport> {
  const kind = QUEUE_KINDS[kindKey];

  // Deliberately NOT wrapped in a try/catch.
  //
  // It used to swallow any discovery failure into an empty queue, which is the
  // exact silent-loss shape this feature is otherwise careful to avoid:
  // treating "cannot ask" as "nothing to do" reports a drained queue while
  // Label Studio is full of work, and looks identical to the legitimate empty
  // state. A failure here should reach the page and be read.
  // `kindKey`, not a literal: the key doubles as the api's URL segment, and
  // hardcoding "laser" here would have made a second queue silently walk the
  // laser projects while labelling itself head/tail.
  const outstanding = await liveProjectIds(kindKey, revalidate);
  if (outstanding.length === 0) {
    return { items: [], scanned: 0, projects: [], notWalked: 0 };
  }

  // Resolved ONE AT A TIME, in the order the policy returned.
  //
  // This used to resolve every outstanding project up front — dozens of
  // parallel calls to hosted Label Studio on page load, which earned a 429 —
  // and then walk at most a handful. Sequential and lazy: two requests per
  // project actually walked, stopping as soon as there is a batch.
  // Newest dive first: project ids ascend with dives, and the recent ones are
  // where labeling is still happening. Sorted here rather than in the shared
  // policy, because the landing page has its own order.
  const candidates = [...outstanding].sort((a, b) => b - a);

  const items: TriageItem[] = [];
  const projects: ProjectOutcome[] = [];
  let scanned = 0;
  let walked = 0;
  let hydrations = 0;

  for (const projectId of candidates) {
    if (items.length >= want) break;
    // Two separate budgets, because a project can be resolved and then not
    // walked. Capping only the walks would let a long run of unpublished or
    // finished projects fan out over Label Studio on one page load; capping
    // only the resolutions is what used to happen, and a run of them at the
    // front of the list consumed the whole budget so triage reported an empty
    // queue while older projects held work.
    if (walked >= MAX_PROJECTS_WALKED_PER_LOAD) break;
    if (scanned >= MAX_PROJECTS_RESOLVED_PER_LOAD) break;
    scanned += 1;

    const outcome: ProjectOutcome = { projectId, tasks: 0, taken: 0, reasons: {} };
    projects.push(outcome);

    let project;
    try {
      project = await getProject(projectId, revalidate);
    } catch (error) {
      // A legacy id that no longer resolves must not take down the page.
      outcome.error = error instanceof Error ? error.message : "unresolvable";
      continue;
    }
    outcome.title = project.title;
    if (!isPublished(project)) {
      outcome.error = "unpublished in Label Studio";
      continue;
    }
    // The same narrowing the landing page applies, from the same fetch — see
    // `hasOutstandingTasks`. Walking a finished project is not wrong, just
    // wasted: every task refuses on `is_labeled`, at the cost of a request and
    // a slot in the walk budget.
    if (!hasOutstandingTasks(project)) {
      outcome.error = "finished in Label Studio";
      continue;
    }

    walked += 1;
    const page = await listTasks(project.id, 1);
    outcome.tasks = page.tasks.length;
    for (const listed of page.tasks) {
      if (items.length >= want) break;

      // Cheap refusal first, straight off the list row. `is_labeled` IS on the
      // list response and excludes everything the gate or a human has already
      // handled — most of a swept dive — so the hydration budget is spent only
      // on tasks that might actually be offered.
      if (listed.is_labeled) {
        outcome.reasons.is_labeled = (outcome.reasons.is_labeled ?? 0) + 1;
        continue;
      }

      if (hydrations >= MAX_HYDRATIONS_PER_LOAD) {
        outcome.truncated = true;
        break;
      }

      // The list row carries no predictions. Judging it directly reported
      // "no prediction (0 present)" for every task in every project, which is
      // indistinguishable from an empty queue and is exactly what it looked
      // like.
      hydrations += 1;
      const task = await getTask(listed.id);
      if (!task) {
        outcome.reasons["task vanished"] = (outcome.reasons["task vanished"] ?? 0) + 1;
        continue;
      }

      const reason = rejectionReason(task, kind, EMPTY);
      if (reason) {
        const key = reasonKey(reason);
        outcome.reasons[key] = (outcome.reasons[key] ?? 0) + 1;
        continue;
      }
      outcome.taken += 1;
      const prediction = pickPrediction(task)!;
      const keypoints = keypointsOf(prediction);
      items.push({
        taskId: task.id,
        projectId: project.id,
        diveName: diveNameFromTitle(project.title, kind),
        keypoints,
        result: prediction.result,
        partial: keypoints.length < kind.expectedKeypoints,
      });
    }
  }

  return {
    items,
    scanned,
    projects,
    notWalked: Math.max(0, candidates.length - scanned),
  };
}

/** Server-side load knows nothing about this session's skips — the client
 *  filters those out itself, so the same task is never re-shown. */
const EMPTY: ReadonlySet<number> = new Set<number>();

export { isTriageable };
