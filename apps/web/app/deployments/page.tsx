import Link from "next/link";
import { chooseTenant } from "@/lib/captures";
import { getMyTenants, type TenantMembership } from "@/lib/fishsense-api";
import { getDeployments, type Deployment } from "@/lib/mission-facts";
import { requireSignedIn } from "../captures/guard";
import { deploymentHref } from "../missions/mission-table";
import { NewDeploymentForm } from "./new-deployment-form";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ tenant?: string }> };

function dates(d: Deployment): string {
  if (!d.started_on) return "Undated";
  if (!d.ended_on || d.ended_on === d.started_on) return d.started_on;
  return `${d.started_on} – ${d.ended_on}`;
}

/**
 * A tenant's deployments: its field trips, each holding missions across
 * expedition days (PLAN.md §4.9, Phase 2b). Missions from before e4edm
 * belong to none until someone groups them.
 */
export default async function DeploymentsPage({ searchParams }: Props) {
  const { tenant: requested } = await searchParams;
  const session = await requireSignedIn(
    `/deployments${requested ? `?tenant=${encodeURIComponent(requested)}` : ""}`,
  );
  const token = session.accessToken ?? "";

  let tenants: TenantMembership[] = [];
  let tenant: TenantMembership | null = null;
  let deployments: Deployment[] = [];
  let failure: string | null = null;
  try {
    tenants = await getMyTenants(token);
    tenant = chooseTenant(tenants, requested);
    if (tenant) deployments = await getDeployments(token, tenant.slug);
  } catch (error) {
    failure = error instanceof Error ? error.message : "unavailable";
  }

  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Deployments</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            Field trips, each holding the missions recorded on it.{" "}
            {tenant && (
              <>
                <Link href={`/missions?tenant=${tenant.slug}`} className="text-sky-700 hover:underline dark:text-sky-400">
                  All missions
                </Link>
                {" · "}
                <Link href={`/captures?tenant=${tenant.slug}`} className="text-sky-700 hover:underline dark:text-sky-400">
                  Mobile captures
                </Link>
              </>
            )}
          </p>
        </div>
        {tenants.length > 1 && (
          <nav aria-label="Tenant" className="flex flex-wrap gap-2">
            {tenants.map((t) => (
              <Link
                key={t.slug}
                href={`/deployments?tenant=${t.slug}`}
                aria-current={t.slug === tenant?.slug ? "page" : undefined}
                className="rounded-md border border-slate-300 px-3 py-1 text-sm aria-[current=page]:border-sky-600 aria-[current=page]:bg-sky-50 dark:border-slate-700 dark:aria-[current=page]:bg-sky-950"
              >
                {t.name}
              </Link>
            ))}
          </nav>
        )}
      </header>

      {failure ? (
        <p className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-950 dark:text-red-300">
          Could not load deployments: {failure}
        </p>
      ) : !tenant ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">
          This account isn&apos;t a member of any tenant yet.
        </p>
      ) : (
        <div className="space-y-8">
          {deployments.length === 0 ? (
            <p className="text-sm text-slate-600 dark:text-slate-400">No deployments yet.</p>
          ) : (
            <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 dark:divide-slate-800 dark:border-slate-800">
              {deployments.map((d) => (
                <li key={d.number}>
                  <Link
                    href={deploymentHref(tenant.slug, d.number)}
                    className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3 hover:bg-slate-50 dark:hover:bg-slate-900"
                  >
                    <span className="font-medium">{d.name}</span>
                    <span className="text-sm text-slate-500">
                      {dates(d)} · {d.missions} {d.missions === 1 ? "mission" : "missions"}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
          {tenant.isAdmin && <NewDeploymentForm slug={tenant.slug} />}
        </div>
      )}
    </main>
  );
}
