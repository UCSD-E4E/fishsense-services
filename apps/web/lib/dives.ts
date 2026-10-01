/**
 * Dives and their calibration-source links, through the v2 API.
 *
 * Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/dives.ts.
 *
 * v2 changes: tenant-scoped routes through the generated client; the
 * signed-in user's own token (the API checks their admin role itself) instead
 * of v1's shared Basic-auth account; dives are addressed by `number` (v1's id
 * for a migrated dive), and the API's reason for a refusal is kept.
 */
import { apiClient, failure, type Schemas } from "./api/client";
import { tenantSlug } from "./env";

/** A dive as the portal sees it. v1's `id`, `dive_datetime`, `dive_slate_id`,
 *  `calibration_target_id` and `calibration_dive_id` are `number`, `dived_at`,
 *  `slate_template_number`, `calibration_target_number` and
 *  `calibration_source_number`. */
export type Dive = Schemas["Dive"];

/** Validate a dive number before it goes into a request URL.
 *
 * These originate client-side (a `<select>` value passed through a server
 * action), so TypeScript's `number` type is no runtime guarantee. Coercing
 * through `Number()` and constraining to a non-negative integer stops anything
 * untrusted from injecting extra path segments or steering the request
 * elsewhere (js/request-forgery). The returned value is a plain number — safe
 * to interpolate as a single path segment. */
function safeId(value: number, label: string): number {
  const id = Number(value);
  if (!Number.isInteger(id) || id < 0) {
    throw new Error(`Invalid ${label}: ${value}`);
  }
  return id;
}

/** Every dive in the tenant, for the calibration-linking table. */
export async function getDives(accessToken: string, revalidate = 0): Promise<Dive[]> {
  const { data, error, response } = await apiClient(accessToken, { revalidate }).GET(
    "/tenants/{slug}/dives",
    { params: { path: { slug: tenantSlug() } } },
  );
  if (data === undefined) {
    console.error("[dives] list fetch failed", {
      url: response.url,
      status: response.status,
      statusText: response.statusText,
    });
    throw failure("fishsense-api dives list failed", response, error);
  }
  return data;
}

/** Link dive `diveNumber` to borrow dive `sourceNumber`'s laser calibration. */
export async function setCalibrationSource(
  accessToken: string,
  diveNumber: number,
  sourceNumber: number,
): Promise<void> {
  const number = safeId(diveNumber, "diveNumber");
  const source_number = safeId(sourceNumber, "sourceNumber");
  const { data, error, response } = await apiClient(accessToken).PUT(
    "/tenants/{slug}/dives/{number}/calibration-source/{source_number}",
    { params: { path: { slug: tenantSlug(), number, source_number } } },
  );
  if (data === undefined) {
    console.error("[dives] set calibration source failed", {
      url: response.url,
      status: response.status,
      statusText: response.statusText,
    });
    throw failure("set calibration source failed", response, error);
  }
}

/** Remove any borrowed-calibration link from dive `diveNumber` (idempotent). */
export async function clearCalibrationSource(
  accessToken: string,
  diveNumber: number,
): Promise<void> {
  const number = safeId(diveNumber, "diveNumber");
  const { error, response } = await apiClient(accessToken).DELETE(
    "/tenants/{slug}/dives/{number}/calibration-source",
    { params: { path: { slug: tenantSlug(), number } } },
  );
  if (!response.ok) {
    console.error("[dives] clear calibration source failed", {
      url: response.url,
      status: response.status,
      statusText: response.statusText,
    });
    throw failure("clear calibration source failed", response, error);
  }
}
