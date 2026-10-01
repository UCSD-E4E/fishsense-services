/**
 * What the portal asks the v2 API about Label Studio projects and the caller.
 *
 * Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/fishsense-api.ts.
 *
 * v2 changes:
 *  * one tenant-scoped route, `GET /tenants/{slug}/label-studio-projects`,
 *    through the generated client (v1: four `/api/v1/labels/{kind}/...`
 *    routes, hand-typed);
 *  * a bearer token for the web's Authentik service account, not v1's
 *    Basic-auth password -- re-minted once if the API refuses it;
 *  * `getMyMembership`: the caller's role in the tenant, which is what the
 *    portal's gate is now made of.
 */
import { ApiError, apiClient, failure, type Schemas } from "./api/client";
import { tenantSlug } from "./env";
import { getServiceToken } from "./service-token";

export { ApiError };

/** The web's names for the label kinds (also the triage `?kind=` values). */
type LabelKind = "laser" | "species" | "headtail" | "dive-slate";

type ApiKind = "laser" | "head_tail" | "species" | "slate";

/** v2 spells the kinds as `label_studio_projects.kind` does. */
const API_KIND: Record<LabelKind, ApiKind> = {
  laser: "laser",
  species: "species",
  headtail: "head_tail",
  "dive-slate": "slate",
};

// Kinds whose predictions carry an auto-accept gate verdict.
//
// Only laser has one: `laser_predictions.gate_verdict` is the sole gate field.
// Add a kind here when its predictions grow one — head/tail is the next
// candidate.
//
// v1 had no `gated` parameter on the other kinds' routes; v2's one route
// refuses it for them (422) rather than silently blanking the section, which
// is why this is a per-kind opt-in rather than a blanket parameter.
const GATED_KINDS: ReadonlySet<LabelKind> = new Set<LabelKind>(["laser"]);

export async function getProjectIds(kind: LabelKind, revalidate: number): Promise<number[]> {
  // `gated=true` hides projects the auto-accept gate has not finished with.
  // Those projects' pending frames are the machine's work, not a labeler's:
  // the gate is about to accept them, so a human who judges them first has
  // done the work twice. The API's predicate is "the gate is done here", not
  // "the gate has run here" — a half-swept dive still holds frames it is
  // about to take.
  return askProjectIds(kind, revalidate, {
    incomplete: true,
    ...(GATED_KINDS.has(kind) ? { gated: true } : {}),
  });
}

/**
 * Every Label Studio project of `kind` the tenant has, finished or not.
 *
 * v2: what `lib/tenant-tasks.ts` checks a caller's task id against. Every
 * tenant's projects share one Label Studio workspace, so "Label Studio has
 * this task" says nothing about whose it is. Not `getProjectIds`: a project
 * a labeler has just finished, or one the gate still holds, is the tenant's
 * all the same.
 */
export async function getTenantProjectIds(
  kind: LabelKind,
  revalidate: number,
): Promise<number[]> {
  return askProjectIds(kind, revalidate, {});
}

async function askProjectIds(
  kind: LabelKind,
  revalidate: number,
  filters: { incomplete?: boolean; gated?: boolean },
): Promise<number[]> {
  const query = { kind: API_KIND[kind], ...filters };
  const ask = async (token: string) =>
    apiClient(token, { revalidate }).GET("/tenants/{slug}/label-studio-projects", {
      params: { path: { slug: tenantSlug() }, query },
    });

  let result = await ask(await getServiceToken());
  if (result.response.status === 401) {
    // The cached token was revoked or expired early: mint once and ask again.
    result = await ask(await getServiceToken(true));
  }

  if (result.data === undefined) {
    const error = failure(`fishsense-api ${kind} project IDs failed`, result.response, result.error);
    console.error(`[fishsense-api] ${kind} project IDs fetch failed`, {
      url: result.response.url,
      status: result.response.status,
      statusText: result.response.statusText,
    });
    throw error;
  }
  return result.data;
}

export type Membership = { role: string; isAdmin: boolean };

/**
 * The signed-in user's membership in the tenant, asked as them.
 *
 * `null` means the API answered "not a member" (it says 404 for that and for
 * "no such tenant" alike). Anything else that is not an answer -- a refused
 * token, an outage -- throws, so the caller cannot mistake it for one.
 */
export async function getMyMembership(accessToken: string): Promise<Membership | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/membership",
    { params: { path: { slug: tenantSlug() } } },
  );
  if (data !== undefined) {
    const membership: Schemas["MyMembership"] = data;
    return { role: membership.role, isAdmin: membership.is_admin === true };
  }
  if (response.status === 404) return null;
  throw failure("fishsense-api membership failed", response, error);
}

export type { LabelKind };
