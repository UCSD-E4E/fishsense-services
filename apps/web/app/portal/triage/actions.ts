"use server";

// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/portal/triage/actions.ts.
// v2 changes: "authorized" is the tenant admin role from the v2 API
// (lib/authz.ts), read from the session as v1 read its groups; and the task
// must be the tenant's (lib/tenant-tasks.ts), since every tenant's projects
// share one Label Studio workspace. Label Studio is written directly, as in
// v1, so these checks are the only gate on it.

import { auth } from "@/auth";
import { isPortalAuthorized } from "@/lib/authz";
import { acceptPrediction, deleteAnnotation } from "@/lib/label-studio-tasks";
import { requireTenantAnnotation, requireTenantTask } from "@/lib/tenant-tasks";
import type { LsRegion } from "@/lib/triage";

export type AcceptResult = { ok: true; annotationId: number } | { ok: false; error: string };
export type UndoResult = { ok: true } | { ok: false; error: string };

/** Server actions are public endpoints. Re-check authorization on every call
 *  rather than trusting that the page only renders them for permitted users. */
async function requireAuthorized(): Promise<void> {
  const session = await auth();
  if (!session?.user) throw new Error("Not authenticated");
  if (!isPortalAuthorized(session)) throw new Error("Not authorized");
}

/**
 * Accept: post the prediction's regions verbatim.
 *
 * The client sends back the exact `result` array the server handed it. That
 * round trip is deliberate — reconstructing the regions browser-side would put
 * a second chance to corrupt them between the model and the database, and
 * `sync_laser_labels_for_label_studio_project_activity` reads those fields
 * straight back out.
 */
export async function acceptAction(
  taskId: number,
  result: LsRegion[],
  leadTimeMs: number,
): Promise<AcceptResult> {
  try {
    await requireAuthorized();
    await requireTenantTask(taskId);
    const annotationId = await acceptPrediction(taskId, result, leadTimeMs);
    return { ok: true, annotationId };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Accept failed" };
  }
}

/** Undo an accept from this session. There is no skip counterpart: skip
 *  writes nothing, so there is nothing to take back. */
export async function undoAcceptAction(annotationId: number): Promise<UndoResult> {
  try {
    await requireAuthorized();
    await requireTenantAnnotation(annotationId);
    await deleteAnnotation(annotationId);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Undo failed" };
  }
}
