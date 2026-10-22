/**
 * Every measured fish, Lite and Mobile alike, as the web shows it (PLAN.md
 * §4.9, Phase 1 of §9.26): `GET /tenants/{slug}/observations` and its detail.
 *
 * Every call is made as the signed-in user, so the API decides what they may
 * see. A tenant slug, capture id, fish id and device filter that came from
 * the URL are validated before they go into an API path.
 */
import { paths } from "./tenant-paths";
import { apiClient, failure, type Schemas } from "./api/client";
import { safeSlug, safeUuid } from "./ids";

export type ObservationPage = Schemas["ObservationPage"];
export type Observation = Schemas["ObservationView"];
export type ObservationDetail = Schemas["ObservationDetailView"];
/** `other` is every other kind of device, or none: the three together cover
 *  every fish once. */
export type Device = "lite" | "mobile" | "other";

/** The `?device=` filters the API knows; anything else means no filter. */
export function deviceFilter(value: string | undefined): Device | undefined {
  return value === "lite" || value === "mobile" || value === "other"
    ? value
    : undefined;
}

/** One page of the tenant's fish, newest first. */
export async function getObservationPage(
  accessToken: string,
  slug: string,
  options: { device?: Device; before?: string; mission?: number; limit?: number; page?: number } = {},
): Promise<ObservationPage> {
  const query: { device?: Device; before?: string; mission?: number; limit?: number; page?: number } = {};
  if (options.device) query.device = options.device;
  // The API bounds it (and says so in its schema); a number can't forge a path.
  if (options.limit !== undefined) query.limit = options.limit;
  if (options.mission !== undefined) {
    if (!Number.isInteger(options.mission) || options.mission < 0) {
      throw new Error(`Invalid mission number: ${options.mission}`);
    }
    query.mission = options.mission;
  }
  if (options.before) query.before = options.before;
  if (options.page !== undefined) query.page = options.page;
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/observations",
    { params: { path: { slug: safeSlug(slug) }, query } },
  );
  if (data === undefined)
    throw failure("fishsense-api observations failed", response, error);
  return data;
}

/** One fish in one capture, with its evidence; null when there is none (the
 *  API says 404 for "not in your tenant" too). */
export async function getObservation(
  accessToken: string,
  slug: string,
  captureId: string,
  fishId: string,
): Promise<ObservationDetail | null> {
  const { data, error, response } = await apiClient(accessToken).GET(
    "/tenants/{slug}/observations/{capture_id}/{fish_id}",
    {
      params: {
        path: {
          slug: safeSlug(slug),
          capture_id: safeUuid(captureId, "capture"),
          fish_id: safeUuid(fishId, "fish"),
        },
      },
    },
  );
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api observation failed", response, error);
}

/** What the fish is: a species by its names, a fish model as one. */
export function fishName(o: Observation): {
  name: string;
  detail: string | null;
} {
  // A species has either name, or both (the API sends none without one).
  const common = o.species?.common_name ?? null;
  const scientific = o.species?.scientific_name ?? null;
  if (common) return { name: common, detail: scientific };
  if (scientific) return { name: scientific, detail: null };
  if (o.fish_model) return { name: o.fish_model.name, detail: "fish model" };
  return { name: "Unidentified fish", detail: null };
}

/** A fish model's measurement against its known length, as a fraction
 *  (-0.025 is 2.5% short); null when there is nothing to compare. */
export function knownLengthError(o: Observation): number | null {
  const known = o.fish_model?.known_length_m;
  if (known == null || o.length_m == null) return null;
  return (o.length_m - known) / known;
}

/** An error as a fraction, signed, to a tenth of a percent ("+1.7%"). */
export function formatError(error: number): string {
  const percent = (error * 100).toFixed(1);
  return error > 0 ? `+${percent}%` : `${percent}%`;
}

/** How the depth was found. */
export function methodLabel(method: string): string {
  switch (method) {
    case "laser":
      return "Laser";
    case "lidar":
      return "LiDAR";
    default:
      return method;
  }
}

/** Where the fish was: its place (Mobile), else its mission (Lite, which
 *  has no place of its own). A phone fish is in a mission too since 0057, so
 *  the place comes first. */
export function where(o: Observation): string | null {
  return o.place_name ?? o.mission?.name ?? null;
}

/** A fish's own page, for Lite and Mobile alike: a mobile fish's page shows
 *  its capture's evidence too (`fish-evidence.ts`). */
export function observationHref(slug: string, o: Observation): string {
  return paths.fishDetail(slug, o.capture_id, o.fish_id);
}
