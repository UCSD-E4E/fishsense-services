import Link from "next/link";
import { unstable_rethrow } from "next/navigation";
import { Suspense } from "react";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { paths } from "@/lib/tenant-paths";
import type { TenantMembership } from "@/lib/fishsense-api";
import {
  Failure,
  FishLoading,
  LineLoading,
  MapLoading,
  NeedsYou,
  NothingYet,
  Numbers,
  reason,
  RecentFish,
  RecentTrips,
  RowsLoading,
  StatsLoading,
  WhereMeasured,
  Work,
  WorkLoading,
} from "./today-sections";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string }> };

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

          <section className="flex flex-col gap-3">
            <h2 className="text-xl font-semibold">Where we&apos;ve measured</h2>
            <Suspense fallback={<MapLoading />}>
              <WhereMeasured token={token} slug={slug} />
            </Suspense>
          </section>

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
