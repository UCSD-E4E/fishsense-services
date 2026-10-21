import Link from "next/link";
import { unstable_rethrow } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { type TenantMembership } from "@/lib/fishsense-api";
import { paths } from "@/lib/tenant-paths";
import { getDeployments, type Deployment } from "@/lib/mission-facts";
import { deploymentHref } from "@/app/[tenant]/data/missions/mission-table";
import { NewDeploymentForm } from "./new-deployment-form";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string }> };

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
export default async function DeploymentsPage({ params }: Props) {
  const { tenant: slug } = await params;
  const session = await requireSignedIn(paths.data(slug));
  const token = session.accessToken ?? "";

  let tenant: TenantMembership | null = null;
  let deployments: Deployment[] = [];
  let failure: string | null = null;
  try {
    tenant = await tenantFor(token, slug);
    deployments = await getDeployments(token, tenant.slug);
  } catch (error) {
    unstable_rethrow(error);
    failure = error instanceof Error ? error.message : "unavailable";
  }

  return (
    <main className="mx-auto max-w-5xl px-6 py-12">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Deployments</h1>
          <p className="mt-1 text-sm text-fg-2">
            Field trips, each holding the missions recorded on it.{" "}
            {tenant && (
              <>
                <Link href={paths.missions(tenant.slug)} className="text-teal-fg hover:underline">
                  All missions
                </Link>
                {" · "}
                <Link href={paths.phoneMissions(tenant.slug)} className="text-teal-fg hover:underline">
                  Phone missions
                </Link>
              </>
            )}
          </p>
        </div>
      </header>

      {failure ? (
        <p className="rounded-card border border-error/40 bg-error/10 p-3 text-sm text-error">
          Could not load deployments: {failure}
        </p>
      ) : !tenant ? (
        <p className="text-sm text-fg-2">
          This account isn&apos;t a member of any tenant yet.
        </p>
      ) : (
        <div className="space-y-8">
          {deployments.length === 0 ? (
            <p className="text-sm text-fg-2">No deployments yet.</p>
          ) : (
            <ul className="divide-y divide-line overflow-hidden rounded-card border border-line bg-surface">
              {deployments.map((d) => (
                <li key={d.number}>
                  <Link
                    href={deploymentHref(tenant.slug, d.number)}
                    className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3 hover:bg-raised"
                  >
                    <span className="font-medium">{d.name}</span>
                    <span className="text-sm text-fg-2">
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
