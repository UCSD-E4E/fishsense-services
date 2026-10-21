import Link from "next/link";
import { notFound } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { byDay, deploymentFacts, provenance } from "@/lib/data-view";
import { isSlug } from "@/lib/ids";
import { getDeployment, purposeLabel } from "@/lib/mission-facts";
import { paths } from "@/lib/tenant-paths";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string; number: string }> };

/**
 * One field trip and its missions, by day (docs/design/README.md, "Web ·
 * Data"): what it holds, how it was made, and each mission's camera and
 * frames (PLAN.md §4.9, Phase 2b).
 */
export default async function DeploymentPage({ params }: Props) {
  const { tenant, number: rawNumber } = await params;
  if (!isSlug(tenant) || !/^\d+$/.test(rawNumber)) notFound();
  const number = Number(rawNumber);
  const session = await requireSignedIn(paths.deployment(tenant, number));
  const deployment = await getDeployment(session.accessToken ?? "", tenant, number);
  if (!deployment) notFound();

  const facts = deploymentFacts(deployment.missions);
  const made = provenance(deployment);
  const dates = deployment.started_on
    ? deployment.ended_on && deployment.ended_on !== deployment.started_on
      ? `${deployment.started_on} – ${deployment.ended_on}`
      : deployment.started_on
    : "Undated";
  const plural = (n: number, one: string) => `${n.toLocaleString()} ${one}${n === 1 ? "" : "s"}`;

  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-6 px-6 py-8">
      <nav aria-label="Breadcrumb" className="text-sm text-fg-2">
        <Link href={paths.data(tenant)} className="text-teal-fg hover:underline">
          Data
        </Link>{" "}
        <span aria-hidden>›</span> Deployments <span aria-hidden>›</span> {deployment.name}
      </nav>

      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-bold tracking-tight">{deployment.name}</h1>
        <div className="flex flex-wrap items-center gap-2 text-fg-2">
          <span>
            {dates} · {plural(facts.missions, "mission")}
            {facts.cameras.length > 0 && ` · ${plural(facts.cameras.length, "camera")}`}
          </span>
          {made && (
            <span className="inline-flex h-7 items-center rounded-chip bg-raised px-2.5 text-sm text-fg">
              {made.label}
            </span>
          )}
        </div>
        {deployment.source_path && (
          <code className="break-all font-mono text-[13px] text-fg-2">{deployment.source_path}</code>
        )}
      </header>

      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
        <Stat label="Frames" value={facts.frames.toLocaleString()} />
        <Stat label="Missions" value={facts.missions.toLocaleString()} />
        <Stat label="Cameras" value={facts.cameras.join(", ") || "—"} small />
      </div>

      {deployment.notes && !made?.kind.startsWith("inferred") && (
        <p className="whitespace-pre-wrap">{deployment.notes}</p>
      )}
      {deployment.readme && (
        <pre className="max-h-80 overflow-auto rounded-card bg-raised p-4 font-mono text-xs">
          {deployment.readme}
        </pre>
      )}

      <section className="flex flex-col gap-3">
        <h2 className="text-xl font-semibold">Missions</h2>
        {deployment.missions.length === 0 ? (
          <p className="text-fg-2">No missions yet.</p>
        ) : (
          byDay(deployment.missions).map((day) => (
            <div key={day.date} className="overflow-hidden rounded-card border border-line bg-surface">
              <div className="flex justify-between bg-raised px-5 py-3 text-sm font-semibold">
                <span>{day.label}</span>
                <span className="font-medium text-fg-2">{plural(day.missions.length, "mission")}</span>
              </div>
              <ul>
                {day.missions.map((m) => (
                  <li key={m.number} className="border-t border-line">
                    <Link
                      href={paths.mission(tenant, m.number)}
                      className="flex flex-wrap items-center gap-x-5 gap-y-1.5 px-5 py-3.5 hover:bg-raised"
                    >
                      <span className="w-11 tabular-nums text-fg-2">{m.number}</span>
                      <span className="min-w-0 flex-[1_1_220px] font-medium">
                        {m.name ?? `Mission ${m.number}`}
                        {m.purpose && (
                          <span className="font-normal text-fg-2"> · {purposeLabel(m.purpose)}</span>
                        )}
                      </span>
                      {m.device_name && (
                        <span className="inline-flex h-6 items-center rounded-chip bg-raised px-2 text-xs font-medium">
                          {m.device_name}
                        </span>
                      )}
                      <span className="w-28 text-right tabular-nums text-fg-2">
                        {plural(m.captures, "frame")}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </section>
    </main>
  );
}

function Stat({ label, value, small = false }: { label: string; value: string; small?: boolean }) {
  return (
    <div className="rounded-card border border-line bg-surface px-5 py-4">
      <div className="text-sm text-fg-2">{label}</div>
      <div className={small ? "mt-1 text-xl font-semibold" : "text-[28px] font-semibold tabular-nums"}>
        {value}
      </div>
    </div>
  );
}
