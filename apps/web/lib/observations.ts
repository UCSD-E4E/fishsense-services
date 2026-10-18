/**
 * Every measured fish, Lite and Mobile alike, as the web shows it (PLAN.md
 * §4.9, Phase 1 of §9.26): `GET /tenants/{slug}/observations` and its detail.
 *
 * Every call is made as the signed-in user, so the API decides what they may
 * see. A tenant slug, capture id, fish id and device filter that came from
 * the URL are validated before they go into an API path.
 */
import { apiClient, failure, type Schemas } from "./api/client";

export type ObservationPage = Schemas["ObservationPage"];
export type Observation = Schemas["ObservationView"];
export type ObservationDetail = Schemas["ObservationDetailView"];
export type Device = "lite" | "mobile";

const SLUG = /^[a-z0-9][a-z0-9-]*$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function safeSlug(slug: string): string {
  if (!SLUG.test(slug))
    throw new Error(`Invalid tenant: ${JSON.stringify(slug)}`);
  return slug;
}

function safeUuid(id: string, what: string): string {
  if (!UUID.test(id))
    throw new Error(`Invalid ${what} id: ${JSON.stringify(id)}`);
  return id;
}

/** The `?device=` filters the API knows; anything else means no filter. */
export function deviceFilter(value: string | undefined): Device | undefined {
  return value === "lite" || value === "mobile" ? value : undefined;
}

/** One page of the tenant's fish, newest first. */
export async function getObservationPage(
  accessToken: string,
  slug: string,
  options: { device?: Device; before?: string } = {},
): Promise<ObservationPage> {
  const query: { device?: Device; before?: string } = {};
  if (options.device) query.device = options.device;
  if (options.before) query.before = options.before;
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
  if (o.species) {
    return o.species.common_name
      ? { name: o.species.common_name, detail: o.species.scientific_name }
      : { name: o.species.scientific_name, detail: null };
  }
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

/** Where the fish was: its dive (Lite), else its place (Mobile). */
export function where(o: Observation): string | null {
  return o.dive?.name ?? o.place_name ?? null;
}

/** A fish's page: a mobile fish's is its capture's, a Lite fish's its own. */
export function observationHref(slug: string, o: Observation): string {
  if (o.client_capture_uuid) return `/captures/${slug}/${o.client_capture_uuid}`;
  return `/fish/${slug}/${o.capture_id}/${o.fish_id}`;
}
