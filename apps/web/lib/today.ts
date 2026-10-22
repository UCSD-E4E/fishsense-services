/**
 * A tenant's Today page (docs/design/README.md, Home pages): its numbers, its
 * recent fish and deployments, and what needs someone.
 */
import { apiClient, failure, type Schemas } from "./api/client";
import { safeSlug } from "./ids";
import type { Deployment } from "./mission-facts";
import { MAP, project } from "./public-page";

export type TenantSummary = Schemas["TenantSummaryView"];
export type TenantRegions = Schemas["TenantRegionsView"];
export type TenantRegion = Schemas["TenantRegionView"];

export async function getTenantSummary(accessToken: string, slug: string): Promise<TenantSummary> {
  const { data, error, response } = await apiClient(accessToken).GET("/tenants/{slug}/summary", {
    params: { path: { slug: safeSlug(slug) } },
  });
  if (data === undefined) throw failure("fishsense-api summary failed", response, error);
  return data;
}

/** The newest trips first, by their start; an undated trip after the dated
 *  ones, newest-numbered first. */
export function recentDeployments(deployments: Deployment[], n: number): Deployment[] {
  return [...deployments]
    .sort((a, b) => {
      if (a.started_on && b.started_on) return b.started_on.localeCompare(a.started_on);
      if (a.started_on || b.started_on) return a.started_on ? -1 : 1;
      return b.number - a.number;
    })
    .slice(0, n);
}

/** Where the tenant's fish were measured, by broad region (the Today map). */
export async function getTenantRegions(accessToken: string, slug: string): Promise<TenantRegions> {
  const { data, error, response } = await apiClient(accessToken).GET("/tenants/{slug}/regions", {
    params: { path: { slug: safeSlug(slug) } },
  });
  if (data === undefined) throw failure("fishsense-api regions failed", response, error);
  return data;
}

/** The Today map's shape: wide, for a screenshot. */
export const MAP_ASPECT = 2;
/** The narrowest part of the world it shows, so one region sits in its
 *  surroundings rather than filling the frame with coast. */
const MIN_WIDTH = 0.3;
/** Room around the regions: the frame is this much larger than their spread. */
const PADDING = 1.6;
const WORLD_HEIGHT = ((MAP.north - MAP.south) / 360) * MAP.width;

/**
 * The part of public/world.svg to show, framed to the regions: `x`, `y`,
 * `width` and `height` as fractions of the whole map, and the `aspect` it
 * is drawn at (`MAP_ASPECT`, unless the world itself is narrower).
 */
export function mapFrame(regions: Pick<TenantRegion, "latitude" | "longitude">[]): {
  x: number;
  y: number;
  width: number;
  height: number;
  aspect: number;
} {
  const points = regions.map((r) => project(r.latitude, r.longitude));
  const xs = points.map((p) => p.left / 100);
  const ys = points.map((p) => p.top / 100);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];

  let width = Math.max((maxX - minX) * PADDING, MIN_WIDTH);
  let height = (width * MAP.width) / (MAP_ASPECT * WORLD_HEIGHT);
  const tall = (maxY - minY) * PADDING;
  if (tall > height) {
    height = tall;
    width = (height * MAP_ASPECT * WORLD_HEIGHT) / MAP.width;
  }
  width = Math.min(width, 1);
  height = Math.min(height, 1);
  const clamp = (v: number, size: number) => Math.min(Math.max(v, 0), 1 - size);
  return {
    x: clamp((minX + maxX) / 2 - width / 2, width),
    y: clamp((minY + maxY) / 2 - height / 2, height),
    width,
    height,
    aspect: (width * MAP.width) / (height * WORLD_HEIGHT),
  };
}

/** A region's bubble, in pixels across: by area, so twice the fish is twice
 *  the ink, from 40 (a label still fits) to 120 for the most. */
export function bubbleSize(fish: number, most: number): number {
  return Math.round(40 + 80 * Math.sqrt(fish / Math.max(most, 1)));
}
