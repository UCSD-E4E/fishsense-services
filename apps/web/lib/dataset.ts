/**
 * The whole dataset, for system admins (the owner, 2026-11-11): every
 * tenant's fish by broad region, opted in to the public page or not. The API
 * adds up each tenant's Today map (`GET /admin/regions`) and records the
 * visit to each tenant the admin is not a member of (0056).
 */
import { apiClient, failure } from "./api/client";
import type { MapSource, TenantRegions } from "./today";

/** Every tenant's regions; null for anyone who is not a system admin (the
 *  API's 404). */
export async function getDatasetRegions(
  accessToken: string,
  source: MapSource = "human",
): Promise<TenantRegions | null> {
  const { data, error, response } = await apiClient(accessToken).GET("/admin/regions", {
    params: { query: source === "automatic" ? { source } : {} },
  });
  if (data !== undefined) return data;
  if (response.status === 404) return null;
  throw failure("fishsense-api dataset regions failed", response, error);
}
