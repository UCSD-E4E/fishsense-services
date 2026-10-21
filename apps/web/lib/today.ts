/**
 * A tenant's Today page (docs/design/README.md, Home pages): its numbers, its
 * recent fish and deployments, and what needs someone.
 */
import { apiClient, failure, type Schemas } from "./api/client";
import { safeSlug } from "./ids";
import type { Deployment } from "./mission-facts";

export type TenantSummary = Schemas["TenantSummaryView"];

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
