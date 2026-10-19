"use server";

// The mission and deployment pages' writes (PLAN.md §4.9, Phase 2b).
//
// Server actions are public endpoints, so every call re-checks: signed in,
// an access token refreshed before it is spent (as the calibration portal's
// actions do), and an admin of the tenant the URL names -- these pages span
// a user's tenants, so the session's own role says nothing about this one.
// Values are checked before any request. The API checks the role again and
// refuses a port the owner's rules fix; its words come back to the page.

import { revalidatePath } from "next/cache";
import { auth, unstable_update } from "@/auth";
import { accessTokenIsFresh } from "@/lib/authz";
import { getMyTenants } from "@/lib/fishsense-api";
import { safeSlug } from "@/lib/ids";
import {
  createDeployment,
  parseMissionNumber,
  parsePort,
  parsePurpose,
  setMissionDeployment,
  setMissionPort,
  setMissionPurpose,
} from "@/lib/mission-facts";

export type ActionResult = { ok: true } | { ok: false; error: string };

export type DeploymentForm = {
  name: string;
  started_on: string;
  ended_on: string;
  notes: string;
};

const EXPIRED = "Your sign-in has expired. Reload the page to sign in again.";
const DATE = /^\d{4}-\d{2}-\d{2}$/;

class Refused extends Error {}

async function requireTenantAdmin(slug: string): Promise<string> {
  let session = await auth();
  if (!session?.user) throw new Refused("Not authenticated");
  if (!accessTokenIsFresh(session)) {
    session = await unstable_update({});
    if (!session || session.error || !accessTokenIsFresh(session)) {
      throw new Refused(EXPIRED);
    }
  }
  const token = session.accessToken!;
  const tenants = await getMyTenants(token);
  if (!tenants.some((t) => t.slug === slug && t.isAdmin)) {
    throw new Refused("Not authorized");
  }
  return token;
}

function missionNumber(value: number): number {
  return parseMissionNumber(String(value));
}

function optional(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function optionalDate(value: string, what: string): string | null {
  const date = optional(value);
  if (date !== null && !DATE.test(date)) throw new Error(`Invalid ${what}: ${date}`);
  return date;
}

async function run(write: () => Promise<void>): Promise<ActionResult> {
  try {
    await write();
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Failed" };
  }
}

export async function setPortAction(
  slug: string,
  number: number,
  port: string,
  note: string,
): Promise<ActionResult> {
  return run(async () => {
    const tenant = safeSlug(slug);
    const mission = missionNumber(number);
    const value = parsePort(port);
    const token = await requireTenantAdmin(tenant);
    await setMissionPort(token, tenant, mission, value, optional(note));
    revalidatePath(`/missions/${tenant}/${mission}`);
  });
}

export async function setPurposeAction(
  slug: string,
  number: number,
  purpose: string,
): Promise<ActionResult> {
  return run(async () => {
    const tenant = safeSlug(slug);
    const mission = missionNumber(number);
    const value = parsePurpose(purpose);
    const token = await requireTenantAdmin(tenant);
    await setMissionPurpose(token, tenant, mission, value);
    revalidatePath(`/missions/${tenant}/${mission}`);
  });
}

/** An empty `deployment` moves the mission out of any. */
export async function setDeploymentAction(
  slug: string,
  number: number,
  deployment: string,
): Promise<ActionResult> {
  return run(async () => {
    const tenant = safeSlug(slug);
    const mission = missionNumber(number);
    const target = deployment === "" ? null : parseMissionNumber(deployment);
    const token = await requireTenantAdmin(tenant);
    await setMissionDeployment(token, tenant, mission, target);
    revalidatePath(`/missions/${tenant}/${mission}`);
  });
}

export async function createDeploymentAction(
  slug: string,
  form: DeploymentForm,
): Promise<ActionResult> {
  return run(async () => {
    const tenant = safeSlug(slug);
    const name = optional(form.name);
    if (name === null) throw new Error("A deployment needs a name");
    const body = {
      name,
      started_on: optionalDate(form.started_on, "start date"),
      ended_on: optionalDate(form.ended_on, "end date"),
      notes: optional(form.notes),
    };
    const token = await requireTenantAdmin(tenant);
    await createDeployment(token, tenant, body);
    revalidatePath("/deployments");
  });
}
