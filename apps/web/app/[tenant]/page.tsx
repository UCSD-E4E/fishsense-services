import Link from "next/link";
import { unstable_rethrow } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { memberOf, myTenants } from "@/app/_shell/tenant";
import { WorkSections } from "@/app/_shell/work-sections";
import { getDeployments, type Deployment } from "@/lib/mission-facts";
import { fishName, getObservationPage, observationHref, type Observation } from "@/lib/observations";
import { paths } from "@/lib/tenant-paths";
import { getTenantSummary, recentDeployments, type TenantSummary } from "@/lib/today";
import type { TenantMembership } from "@/lib/fishsense-api";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string }> };

/**
 * A tenant's Today (docs/design/README.md, Home pages): its numbers, its
 * recent fish and deployments, and what needs someone. Staff also see the
 * labelling work and the dashboards here, which the public home no longer
 * lists.
 */
export default async function TodayPage({ params }: Props) {
  const { tenant: slug } = await params;
  const session = await requireSignedIn(paths.tenant(slug));
  const token = session.accessToken ?? "";

  let tenant: TenantMembership | null = null;
  let summary: TenantSummary | null = null;
  let fish: Observation[] = [];
  let trips: Deployment[] = [];
  let failure: string | null = null;
  try {
    tenant = memberOf(await myTenants(token), slug);
    const [s, page, deployments] = await Promise.all([
      getTenantSummary(token, slug),
      getObservationPage(token, slug, {}),
      getDeployments(token, slug),
    ]);
    summary = s;
    fish = page.observations.slice(0, 3);
    trips = recentDeployments(deployments, 3);
  } catch (error) {
    unstable_rethrow(error);
    failure = error instanceof Error ? error.message : "unavailable";
  }
  const staff = tenant?.isAdmin === true;

  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-7 px-6 py-9">
      <header className="flex flex-col gap-1.5">
        <h1 className="text-3xl font-bold tracking-tight">{tenant?.name ?? slug}</h1>
        {tenant?.role && (
          <p className="text-fg-2">
            You are {tenant.role === "admin" ? "an admin" : `a ${tenant.role}`} here.
          </p>
        )}
      </header>

      {failure && (
        <p role="alert" className="rounded-card border border-error/40 bg-error/10 p-4 text-sm text-error">
          Could not load {slug}: {failure}
        </p>
      )}

      {summary && (
        <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
          <Stat
            href={paths.fish(slug)}
            label="Lengths measured"
            value={summary.lengths.toLocaleString()}
            note={`${summary.wild_fish.toLocaleString()} wild fish, ${summary.species} species`}
          />
          <Stat
            href={paths.data(slug)}
            label="Missions"
            value={summary.missions.toLocaleString()}
            note={`in ${summary.deployments.toLocaleString()} ${summary.deployments === 1 ? "deployment" : "deployments"}`}
          />
          <Stat label="Frames" value={summary.frames.toLocaleString()} note="each counted once" />
          <Stat
            label="Latest activity"
            value={summary.latest_activity ? new Date(summary.latest_activity).toLocaleDateString() : "—"}
            note="the newest ingest or sync"
            small
          />
        </div>
      )}

      <div className="flex flex-wrap items-start gap-6">
        <div className="flex min-w-0 flex-[999_1_560px] flex-col gap-6">
          <section className="flex flex-col gap-3">
            <div className="flex items-baseline gap-3">
              <h2 className="text-xl font-semibold">Recent fish</h2>
              <Link href={paths.fish(slug)} className="ml-auto text-sm text-teal-fg hover:underline">
                All fish
              </Link>
            </div>
            {fish.length === 0 ? (
              <p className="text-fg-2">No measured fish yet.</p>
            ) : (
              <ul className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(200px,1fr))]">
                {fish.map((o) => (
                  <li key={`${o.capture_id}/${o.fish_id}`}>
                    <Link
                      href={observationHref(slug, o)}
                      className="flex h-full flex-col overflow-hidden rounded-card border border-line bg-surface hover:border-fg-3"
                    >
                      {o.photo_url ? (
                        // Presigned, short-lived Garage URLs: not for next/image's optimizer.
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={o.photo_url} alt={fishName(o).name} loading="lazy" className="aspect-4/3 w-full object-cover" />
                      ) : (
                        <div className="flex aspect-4/3 items-center justify-center border-b border-dashed border-line bg-raised text-sm text-fg-2">
                          No photo yet
                        </div>
                      )}
                      <div className="flex items-baseline justify-between gap-2 px-3.5 py-3">
                        <span className="text-sm font-semibold">{fishName(o).name}</span>
                        <span className="whitespace-nowrap font-semibold tabular-nums">
                          {o.length_m == null ? "—" : `${o.length_m.toFixed(3)} m`}
                        </span>
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="flex flex-col gap-3">
            <div className="flex items-baseline gap-3">
              <h2 className="text-xl font-semibold">Recent deployments</h2>
              <Link href={paths.data(slug)} className="ml-auto text-sm text-teal-fg hover:underline">
                All data
              </Link>
            </div>
            {trips.length === 0 ? (
              <p className="text-fg-2">No deployments yet.</p>
            ) : (
              <ul className="overflow-hidden rounded-card border border-line bg-surface">
                {trips.map((d, i) => (
                  <li key={d.number} className={i ? "border-t border-line" : ""}>
                    <Link
                      href={paths.deployment(slug, d.number)}
                      className="flex flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3.5 hover:bg-raised"
                    >
                      <span className="min-w-0 flex-[1_1_240px] font-medium">{d.name}</span>
                      <span className="text-sm text-fg-2">
                        {d.missions} {d.missions === 1 ? "mission" : "missions"}
                      </span>
                      <span className="w-28 text-right text-sm text-fg-2">{d.started_on ?? "Undated"}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>

        <aside className="flex min-w-0 flex-[1_1_320px] flex-col gap-4">
          <section className="flex flex-col gap-2 rounded-card border border-line bg-surface p-5">
            <h2 className="text-lg font-semibold">Needs you</h2>
            {summary && staff ? (
              <Link
                href={`${paths.missions(slug)}?port=unknown`}
                className="flex min-h-11 items-center gap-3 hover:underline"
              >
                <span
                  aria-hidden
                  className={`size-2.5 rounded-full ${summary.unknown_ports ? "bg-warn" : "border-2 border-fg-3"}`}
                />
                <span className="flex-1">Missions with an unknown port</span>
                <span className="font-semibold tabular-nums">{summary.unknown_ports}</span>
              </Link>
            ) : (
              <p className="text-sm text-fg-2">Nothing yet. Your unsynced and unmeasured captures will show here.</p>
            )}
          </section>
        </aside>
      </div>

      {staff && (
        <section className="flex flex-col gap-4 border-t border-line pt-7">
          <h2 className="text-xl font-semibold">Labelling and dashboards</h2>
          <WorkSections />
        </section>
      )}
    </main>
  );
}

function Stat({
  label,
  value,
  note,
  href,
  small = false,
}: {
  label: string;
  value: string;
  note: string;
  href?: string;
  small?: boolean;
}) {
  const body = (
    <>
      <div className="text-sm text-fg-2">{label}</div>
      <div className={small ? "mt-1.5 text-xl font-semibold" : "text-[32px] font-semibold tabular-nums"}>
        {value}
      </div>
      <div className="text-sm text-fg-2">{note}</div>
    </>
  );
  const box = "rounded-card border border-line bg-surface px-5 py-4";
  return href ? (
    <Link href={href} className={`${box} hover:border-fg-3`}>
      {body}
    </Link>
  ) : (
    <div className={box}>{body}</div>
  );
}
