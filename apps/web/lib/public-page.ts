/**
 * The signed-out page (docs/design/README.md, Home pages; the owner,
 * 2026-10-20): live totals and wild fish by broad region, from tenants that
 * opted in. The API decides what may be said (`public_store`); this only
 * places it and words it.
 */
import createClient from "openapi-fetch";
import { failure, type Schemas } from "./api/client";
import type { paths } from "./api/schema";
import { env } from "./env";

export type PublicSummary = Schemas["PublicSummaryView"];
export type PublicRegion = Schemas["PublicRegionView"];

/** public/world.svg's frame: equirectangular, Antarctica left out
 *  (scripts/world-map.py). */
export const MAP = { width: 1200, north: 84, south: -58 } as const;

/** Where a latitude and longitude fall on the map, as percentages. */
export function project(latitude: number, longitude: number): { left: number; top: number } {
  const height = ((MAP.north - MAP.south) / 360) * MAP.width;
  const y = ((MAP.north - latitude) / 360) * MAP.width;
  return { left: ((longitude + 180) / 360) * 100, top: (y / height) * 100 };
}

export function regionLabel(region: Pick<PublicRegion, "count" | "under">): string {
  return region.count != null
    ? `about ${region.count.toLocaleString("en-US")} fish`
    : `under ${region.under} fish`;
}

/** Asked as nobody: the page is for anyone. Cached for five minutes. */
export async function getPublicSummary(): Promise<PublicSummary> {
  const client = createClient<paths>({
    baseUrl: env.fishsenseApiUrl,
    fetch: (request: Request) => fetch(request, { next: { revalidate: 300 } }),
  });
  const { data, error, response } = await client.GET("/public/summary");
  if (data === undefined) throw failure("fishsense-api public summary failed", response, error);
  return data;
}
