import Link from "next/link";
import { unstable_rethrow } from "next/navigation";
import { cache, Suspense, type ReactNode } from "react";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { FishGrid } from "@/app/_shell/fish-grid";
import { WorkSections } from "@/app/_shell/work-sections";
import { getDeployments } from "@/lib/mission-facts";
import { getObservationPage } from "@/lib/observations";
import { paths } from "@/lib/tenant-paths";
import { getTenantSummary, recentDeployments } from "@/lib/today";
import { getIsSystemAdmin } from "@/lib/system-admin";
import type { TenantMembership } from "@/lib/fishsense-api";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string }> };

/** The numbers and "Needs you" both read the summary: asked once per request. */
const summaryOf = cache((token: string, slug: string) => getTenantSummary(token, slug));

/**
 * A tenant's Today (docs/design/README.md, Home pages): its numbers, its
 * recent fish and deployments, and what needs someone. Staff also see the
 * labelling work and the dashboards here, which the public home no longer
 * lists.
 *
 * The page streams. Its header comes first and each section follows as its
 * data arrives, so the slowest section no longer holds back the rest. Before
 * this (2026-10-21) the page waited on all of them, and the summary took
 * 12 s on production.
 */
export default async function TodayPage({ params }: Props) {
  const { tenant: slug } = await params;
  const session = await requireSignedIn(paths.tenant(slug));
  const token = session.accessToken ?? "";

  let tenant: TenantMembership | null = null;
  let failure: string | null = null;
  try {
    tenant = await tenantFor(token, slug);
  } catch (error) {
    unstable_rethrow(error);
    failure = reason(error);
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

      {failure && <Failure>Could not load {slug}: {failure}</Failure>}

      {tenant && (
        <>
          <Suspense fallback={<StatsLoading />}>
            <Numbers token={token} slug={slug} />
          </Suspense>

          <div className="flex flex-wrap items-start gap-6">
            <div className="flex min-w-0 flex-[999_1_560px] flex-col gap-6">
              <section className="flex flex-col gap-3">
                <div className="flex items-baseline gap-3">
                  <h2 className="text-xl font-semibold">Recent fish</h2>
                  <Link href={paths.fish(slug)} className="ml-auto text-sm text-teal-fg hover:underline">
                    All fish
                  </Link>
                </div>
                <Suspense fallback={<FishLoading />}>
                  <RecentFish token={token} slug={slug} />
                </Suspense>
              </section>

              <section className="flex flex-col gap-3">
                <div className="flex items-baseline gap-3">
                  <h2 className="text-xl font-semibold">Recent deployments</h2>
                  <Link href={paths.data(slug)} className="ml-auto text-sm text-teal-fg hover:underline">
                    All data
                  </Link>
                </div>
                <Suspense fallback={<RowsLoading />}>
                  <RecentTrips token={token} slug={slug} />
                </Suspense>
              </section>
            </div>

            <aside className="flex min-w-0 flex-[1_1_320px] flex-col gap-4">
              <section className="flex flex-col gap-2 rounded-card border border-line bg-surface p-5">
                <h2 className="text-lg font-semibold">Needs you</h2>
                {staff ? (
                  <Suspense fallback={<LineLoading />}>
                    <NeedsYou token={token} slug={slug} />
                  </Suspense>
                ) : (
                  <NothingYet />
                )}
              </section>
            </aside>
          </div>

          <Suspense fallback={staff ? <WorkLoading /> : null}>
            <Work token={token} staff={staff} />
          </Suspense>
        </>
      )}
    </main>
  );
}

type Section = { token: string; slug: string };

async function Numbers({ token, slug }: Section) {
  let summary;
  try {
    summary = await summaryOf(token, slug);
  } catch (error) {
    unstable_rethrow(error);
    return <Failure>Could not load the numbers: {reason(error)}</Failure>;
  }
  return (
    <div className={STATS_GRID}>
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
  );
}

/** The three newest fish: three asked for, not a page of fifty. */
async function RecentFish({ token, slug }: Section) {
  let fish;
  try {
    fish = (await getObservationPage(token, slug, { limit: 3 })).observations;
  } catch (error) {
    unstable_rethrow(error);
    return <Failure>Could not load the recent fish: {reason(error)}</Failure>;
  }
  return fish.length === 0 ? <p className="text-fg-2">No measured fish yet.</p> : <FishGrid slug={slug} fish={fish} />;
}

async function RecentTrips({ token, slug }: Section) {
  let trips;
  try {
    trips = recentDeployments(await getDeployments(token, slug), 3);
  } catch (error) {
    unstable_rethrow(error);
    return <Failure>Could not load the deployments: {reason(error)}</Failure>;
  }
  if (trips.length === 0) return <p className="text-fg-2">No deployments yet.</p>;
  return (
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
  );
}

async function NeedsYou({ token, slug }: Section) {
  let summary;
  try {
    summary = await summaryOf(token, slug);
  } catch (error) {
    unstable_rethrow(error);
    return <NothingYet />;
  }
  return (
    <Link href={`${paths.missions(slug)}?port=unknown`} className="flex min-h-11 items-center gap-3 hover:underline">
      <span
        aria-hidden
        className={`size-2.5 rounded-full ${summary.unknown_ports ? "bg-warn" : "border-2 border-fg-3"}`}
      />
      <span className="flex-1">Missions with an unknown port</span>
      <span className="font-semibold tabular-nums">{summary.unknown_ports}</span>
    </Link>
  );
}

/** Labelling for staff, and the dashboards for system admins. Label Studio
 *  answers slowly when it answers at all, so this section never holds up
 *  the others. */
async function Work({ token, staff }: { token: string; staff: boolean }) {
  let systemAdmin = false;
  try {
    systemAdmin = await getIsSystemAdmin(token);
  } catch (error) {
    unstable_rethrow(error);
  }
  if (!staff && !systemAdmin) return null;
  return (
    <section className="flex flex-col gap-4 border-t border-line pt-7">
      <h2 className="text-xl font-semibold">{systemAdmin ? "Labelling and dashboards" : "Labelling"}</h2>
      <WorkSections dashboards={systemAdmin} />
    </section>
  );
}

function NothingYet() {
  return <p className="text-sm text-fg-2">Nothing yet. Your unsynced and unmeasured captures will show here.</p>;
}

function Failure({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="rounded-card border border-error/40 bg-error/10 p-4 text-sm text-error">
      {children}
    </p>
  );
}

function reason(error: unknown): string {
  return error instanceof Error ? error.message : "unavailable";
}

// -- while a section loads: its shape, so nothing jumps when it arrives -------

const STATS_GRID = "grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]";
const PULSE = "animate-pulse rounded bg-raised";

function Loading({ what, children }: { what: string; children: ReactNode }) {
  return (
    <div role="status" aria-label={`Loading ${what}`}>
      {children}
    </div>
  );
}

function StatsLoading() {
  return (
    <Loading what="the numbers">
      <div className={STATS_GRID}>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="flex flex-col gap-2 rounded-card border border-line bg-surface px-5 py-4">
            <div className={`${PULSE} h-4 w-28`} />
            <div className={`${PULSE} h-9 w-20`} />
            <div className={`${PULSE} h-4 w-36`} />
          </div>
        ))}
      </div>
    </Loading>
  );
}

function FishLoading() {
  return (
    <Loading what="the recent fish">
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(200px,1fr))]">
        {[0, 1, 2].map((i) => (
          <div key={i} className="overflow-hidden rounded-card border border-line bg-surface">
            <div className="aspect-4/3 w-full animate-pulse bg-raised" />
            <div className="flex justify-between gap-2 px-3.5 py-3">
              <div className={`${PULSE} h-4 w-24`} />
              <div className={`${PULSE} h-4 w-12`} />
            </div>
          </div>
        ))}
      </div>
    </Loading>
  );
}

function RowsLoading() {
  return (
    <Loading what="the recent deployments">
      <div className="overflow-hidden rounded-card border border-line bg-surface">
        {[0, 1, 2].map((i) => (
          <div key={i} className={`flex items-center gap-4 px-5 py-4 ${i ? "border-t border-line" : ""}`}>
            <div className={`${PULSE} h-4 flex-1`} />
            <div className={`${PULSE} h-4 w-20`} />
            <div className={`${PULSE} h-4 w-24`} />
          </div>
        ))}
      </div>
    </Loading>
  );
}

function LineLoading() {
  return (
    <Loading what="what needs you">
      <div className="flex min-h-11 items-center gap-3">
        <div className={`${PULSE} h-4 flex-1`} />
        <div className={`${PULSE} h-4 w-8`} />
      </div>
    </Loading>
  );
}

function WorkLoading() {
  return (
    <Loading what="the labelling">
      <section className="flex flex-col gap-4 border-t border-line pt-7">
        <div className={`${PULSE} h-6 w-40`} />
        <div className={`${PULSE} h-24 w-full`} />
      </section>
    </Loading>
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
