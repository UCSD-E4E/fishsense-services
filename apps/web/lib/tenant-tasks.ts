/**
 * Whether a Label Studio task or annotation is this tenant's.
 *
 * New in v2. v1 served one deployment from one set of projects, so any task
 * id Label Studio had was its own. v2's tenants share one Label Studio
 * workspace (docs/port-plan.md), and triage reaches Label Studio with the
 * portal's own key: without this, an admin of one tenant could annotate,
 * un-annotate or view any task in the workspace by its id. The portal gate
 * (`lib/authz.ts`) says who the caller is; this says the task is theirs.
 *
 * Fails closed: a task Label Studio doesn't have, or doesn't say the project
 * of, is refused; an API that can't say which projects are the tenant's
 * refuses everything rather than admitting anything.
 */
import { getTenantProjectIds, type LabelKind } from "./fishsense-api";
import { getAnnotation, getTask } from "./label-studio-tasks";

/** What a refused caller is told, for a task that isn't there as for one
 *  that is another tenant's: the difference is theirs to learn. */
export const NOT_THIS_TENANTS = "Not this tenant's task";

const KINDS: readonly LabelKind[] = ["laser", "species", "headtail", "dive-slate"];

/** How long the tenant's project list is cached. A project only joins it
 *  (populate creates it), so a stale list can only refuse, briefly, a
 *  project minutes old -- never admit another tenant's. */
const REVALIDATE_SECONDS = 60;

/** Every Label Studio project the tenant has, of every kind. */
export async function tenantProjectIds(): Promise<Set<number>> {
  const ids = await Promise.all(
    KINDS.map((kind) => getTenantProjectIds(kind, REVALIDATE_SECONDS)),
  );
  return new Set(ids.flat());
}

/** Throws `NOT_THIS_TENANTS` unless task `taskId` is in one of the tenant's
 *  projects. */
export async function requireTenantTask(taskId: number): Promise<void> {
  const [task, owned] = await Promise.all([getTask(taskId), tenantProjectIds()]);
  if (typeof task?.project !== "number" || !owned.has(task.project)) {
    throw new Error(NOT_THIS_TENANTS);
  }
}

/** Throws `NOT_THIS_TENANTS` unless annotation `annotationId` is on one of
 *  the tenant's tasks. */
export async function requireTenantAnnotation(annotationId: number): Promise<void> {
  const annotation = await getAnnotation(annotationId);
  if (annotation === null) throw new Error(NOT_THIS_TENANTS);
  await requireTenantTask(annotation.task);
}
