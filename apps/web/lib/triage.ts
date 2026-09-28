// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/triage.ts.
/**
 * Accept/skip triage of model predictions, ported from the `label-studio-mobile`
 * Android app.
 *
 * The whole safety argument of this feature is that **accepting copies the
 * prediction's `result` array verbatim**. Nothing here reconstructs a region
 * field by field. `sync_laser_labels_for_label_studio_project_activity` reads
 * `original_width`, `original_height`, `value.x`, `value.y` and
 * `value.keypointlabels[0]` straight back out of the stored annotation, so a
 * byte-for-byte copy is correct by construction and a rebuilt one is a new
 * thing to get wrong.
 *
 * **Skip writes nothing.** Do not implement it as a Label Studio cancelled
 * annotation. Those carry `result: []`, so the `from_name` filter finds no
 * region and x/y stay NULL — while `laser_label.completed = task.is_labeled`
 * flips true regardless, and the populate step's `_select_unlabeled_images`
 * then excludes that image for good. Skip is client-side only.
 */

export type LsRegion = {
  from_name?: string;
  to_name?: string;
  type?: string;
  original_width?: number;
  original_height?: number;
  image_rotation?: number;
  value?: Record<string, unknown>;
};

export type LsPrediction = { id: number; result: LsRegion[] };

export type LsTask = {
  id: number;
  is_labeled?: boolean;
  annotations?: { id: number }[];
  predictions?: LsPrediction[];
  data?: Record<string, unknown>;
};

export type Keypoint = {
  /** Label Studio stores image regions as PERCENTAGES of the frame, not px. */
  xPercent: number;
  yPercent: number;
  label: string;
  fromName: string;
  originalWidth?: number;
  originalHeight?: number;
};

export type QueueKind = {
  key: "laser" | "headtail";
  label: string;
  /**
   * Suffix of the per-dive project title.
   *
   * Mirrors `LASER_PROJECT_TITLE_SUFFIX` in the api-worker's create activity.
   * Hand-maintained for now; the durable fix is for fishsense-api to serve
   * this vocabulary so no client re-spells it. Renaming one side silently
   * empties this queue.
   */
  titleSuffix: string;
  /**
   * `from_name` values the sync activity accepts. A region on any other
   * control syncs into nothing, so a prediction using one is never offered.
   */
  fromNames: string[];
  /** Regions a complete prediction carries. Used only to flag a partial one. */
  expectedKeypoints: number;
  /**
   * Keypoint labels the sync activity requires before it writes anything.
   *
   * Empty means "any point on the right control will do" — laser labels vary
   * (`Red Laser` / `Green Laser`) and the sync reads whichever it finds.
   *
   * Head/tail is the opposite, and this is why the field exists.
   * `sync_headtail_labels_for_label_studio_project_activity` picks its two
   * regions out by label and writes x/y only when it found BOTH — while
   * `completed = task.is_labeled` flips true regardless, and
   * `_select_unlabeled_images` then excludes that image for good. So a half
   * prediction is the same permanent-exclusion trap that is the reason Skip
   * writes nothing, and it has to be refused rather than offered as partial.
   */
  requiredLabels: string[];
};

/**
 * The kinds a human can triage.
 *
 * Head/tail joined laser once its predict stage reached prod: the screen is
 * the same and the machinery was already parameterised, so this is the whole
 * of what the second queue needed.
 *
 * Each entry re-spells vocabulary that belongs to the api-worker — the project
 * title suffix, the control name, the keypoint labels. Renaming one side
 * silently empties the queue on this side, which is why the parity is stated
 * as tests rather than left to a comment.
 */
export const QUEUE_KINDS: Record<QueueKind["key"], QueueKind> = {
  laser: {
    key: "laser",
    label: "Laser",
    titleSuffix: "Laser Calibration Labeling",
    fromNames: ["laser", "kp-1"],
    expectedKeypoints: 1,
    requiredLabels: [],
  },
  headtail: {
    key: "headtail",
    label: "Head/tail",
    titleSuffix: "HeadTail Labeling",
    // One `KeyPointLabels name="kp-1"` control carries both choices, and the
    // sync filters on exactly that control.
    fromNames: ["kp-1"],
    expectedKeypoints: 2,
    requiredLabels: ["Snout", "Fork"],
  },
};

export function matchesProjectTitle(title: string, kind: QueueKind): boolean {
  return title.trimEnd().endsWith(kind.titleSuffix);
}

/**
 * The dive's display name, recovered from `"{dive.name} #{dive_id} - {suffix}"`.
 *
 * The `#{dive_id}` is deliberately kept: dive names are not unique in prod, so
 * it is the only thing distinguishing two projects that would otherwise read
 * identically to a labeler.
 */
export function diveNameFromTitle(title: string, kind: QueueKind): string {
  const trimmed = title.trimEnd();
  if (!trimmed.endsWith(kind.titleSuffix)) return trimmed;
  let head = trimmed.slice(0, trimmed.length - kind.titleSuffix.length).trimEnd();
  if (head.endsWith("-")) head = head.slice(0, -1);
  return head.trim();
}

export function keypointsOf(prediction: LsPrediction): Keypoint[] {
  return (prediction.result ?? [])
    .filter((r) => r.type === "keypointlabels" && r.value)
    .map((r) => {
      const value = r.value as {
        x?: number;
        y?: number;
        keypointlabels?: string[];
      };
      return {
        xPercent: Number(value.x ?? 0),
        yPercent: Number(value.y ?? 0),
        label: value.keypointlabels?.[0] ?? "",
        fromName: r.from_name ?? "",
        originalWidth: r.original_width,
        originalHeight: r.original_height,
      };
    });
}

/** The newest prediction that actually carries regions, or null. */
export function pickPrediction(task: LsTask): LsPrediction | null {
  const withRegions = (task.predictions ?? []).filter((p) => (p.result ?? []).length > 0);
  return withRegions.length > 0 ? withRegions[withRegions.length - 1] : null;
}

/**
 * Why [task] cannot be triaged, or null if it can.
 *
 * Split out from a boolean so a task vanishing from the queue is explainable —
 * "queue empty" while Label Studio holds work is otherwise undiagnosable.
 */
export function rejectionReason(
  task: LsTask,
  kind: QueueKind,
  skipped: ReadonlySet<number>,
): string | null {
  if ((task.annotations ?? []).length > 0) return `task ${task.id}: already annotated`;
  if (task.is_labeled) return `task ${task.id}: is_labeled`;
  if (skipped.has(task.id)) return `task ${task.id}: skipped in this session`;
  if (!task.data || typeof task.data.image !== "string" || !task.data.image) {
    return `task ${task.id}: no image in task data`;
  }

  const prediction = pickPrediction(task);
  if (!prediction) {
    return `task ${task.id}: no prediction (${(task.predictions ?? []).length} present)`;
  }

  const keypoints = keypointsOf(prediction);
  if (keypoints.length === 0) return `task ${task.id}: prediction has no keypoints`;

  const wrongControl = [
    ...new Set(keypoints.filter((k) => !kind.fromNames.includes(k.fromName)).map((k) => k.fromName)),
  ];
  if (wrongControl.length > 0) {
    return `task ${task.id}: from_name ${wrongControl.join(",")} not in ${kind.fromNames.join(",")}`;
  }

  // Presence, not count. Two snouts is two keypoints and no fish, and a count
  // check would wave it through into the exclusion trap described on
  // `requiredLabels`.
  const present = new Set(keypoints.map((k) => k.label));
  const missing = kind.requiredLabels.filter((label) => !present.has(label));
  if (missing.length > 0) {
    return `task ${task.id}: prediction is missing ${missing.join(",")}`;
  }

  return null;
}

export function isTriageable(
  task: LsTask,
  kind: QueueKind,
  skipped: ReadonlySet<number>,
): boolean {
  return rejectionReason(task, kind, skipped) === null;
}

/** True when the model produced fewer regions than this kind expects. */
export function isPartial(prediction: LsPrediction, kind: QueueKind): boolean {
  return keypointsOf(prediction).length < kind.expectedKeypoints;
}
