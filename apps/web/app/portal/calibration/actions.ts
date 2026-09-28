"use server";

// Ported from fishsense-lite@77e8f8e5
// apps/fishsense-lite-web/app/portal/calibration/actions.ts.
//
// v2 changes: "authorized" is the tenant admin role the v2 API reports, the
// write is made as the signed-in user (and the API checks the role again),
// and an expired access token is refreshed here -- a server action can write
// the session cookie -- before it is spent.

import { revalidatePath } from "next/cache";
import { auth, unstable_update } from "@/auth";
import { accessTokenIsFresh, isPortalAuthorized } from "@/lib/authz";
import { clearCalibrationSource, setCalibrationSource } from "@/lib/dives";

export type ActionResult = { ok: true } | { ok: false; error: string };

const EXPIRED = "Your sign-in has expired. Reload the page to sign in again.";

/** Server actions are public endpoints — re-check on every call rather than
 * trusting that the client only renders them for permitted users.
 *
 * Checks authorization, not just authentication. The page-level guard is a
 * rendering decision; this is what protects the write on this side, since a
 * server action can be invoked directly by anyone who can reach the app.
 * Returns the access token the write is to carry. */
async function requireAuthorized(): Promise<string> {
  let session = await auth();
  if (!session?.user) {
    throw new Error("Not authenticated");
  }
  if (!isPortalAuthorized(session)) {
    throw new Error("Not authorized");
  }
  if (!accessTokenIsFresh(session)) {
    // Refreshing also re-reads the role, so it is checked again below.
    session = await unstable_update({});
    if (session?.error || !accessTokenIsFresh(session)) {
      throw new Error(EXPIRED);
    }
    if (!isPortalAuthorized(session)) {
      throw new Error("Not authorized");
    }
  }
  return session!.accessToken!;
}

export async function setCalibrationSourceAction(
  diveNumber: number,
  sourceNumber: number,
): Promise<ActionResult> {
  try {
    const token = await requireAuthorized();
    if (diveNumber === sourceNumber) {
      return { ok: false, error: "A dive cannot be its own calibration source" };
    }
    await setCalibrationSource(token, diveNumber, sourceNumber);
    revalidatePath("/portal");
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Failed" };
  }
}

export async function clearCalibrationSourceAction(
  diveNumber: number,
): Promise<ActionResult> {
  try {
    const token = await requireAuthorized();
    await clearCalibrationSource(token, diveNumber);
    revalidatePath("/portal");
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Failed" };
  }
}
