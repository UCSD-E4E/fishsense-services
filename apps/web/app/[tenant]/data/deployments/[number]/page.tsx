import Link from "next/link";
import { notFound } from "next/navigation";
import { isSlug } from "@/lib/ids";
import { getDeployment } from "@/lib/mission-facts";
import { requireSignedIn } from "@/app/_shell/guard";
import { MissionTable } from "@/app/[tenant]/data/missions/mission-table";
import { paths } from "@/lib/tenant-paths";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string; number: string }> };

/** One field trip and its missions (PLAN.md §4.9, Phase 2b). */
export default async function DeploymentPage({ params }: Props) {
  const { tenant, number: rawNumber } = await params;
  if (!isSlug(tenant) || !/^\d+$/.test(rawNumber)) notFound();
  const number = Number(rawNumber);
  const session = await requireSignedIn(`${paths.data(tenant)}/deployments/${number}`);
  const deployment = await getDeployment(session.accessToken ?? "", tenant, number);
  if (!deployment) notFound();

  const dates = deployment.started_on
    ? deployment.ended_on && deployment.ended_on !== deployment.started_on
      ? `${deployment.started_on} – ${deployment.ended_on}`
      : deployment.started_on
    : "Undated";

  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-sm text-slate-500">Deployment {deployment.number}</p>
          <h1 className="text-3xl font-semibold tracking-tight">{deployment.name}</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            {dates} · {deployment.missions.length}{" "}
            {deployment.missions.length === 1 ? "mission" : "missions"}
          </p>
          {deployment.source_path && (
            <code className="mt-1 block break-all text-xs text-slate-500">
              {deployment.source_path}
            </code>
          )}
        </div>
        <Link
          href={paths.data(tenant)}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
        >
          All deployments
        </Link>
      </header>

      {deployment.notes && (
        <p className="mb-6 whitespace-pre-wrap text-sm">{deployment.notes}</p>
      )}
      {deployment.readme && (
        <pre className="mb-6 max-h-80 overflow-auto rounded-md bg-slate-50 p-4 text-xs dark:bg-slate-900">
          {deployment.readme}
        </pre>
      )}

      {deployment.missions.length === 0 ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">No missions yet.</p>
      ) : (
        <MissionTable slug={tenant} missions={deployment.missions} showDeployment={false} />
      )}
    </main>
  );
}
