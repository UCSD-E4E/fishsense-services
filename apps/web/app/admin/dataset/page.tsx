import { notFound } from "next/navigation";
import { Suspense } from "react";
import { MapLoading, MapSourceToggle } from "@/app/[tenant]/today-sections";
import { requireSignedIn } from "@/app/_shell/guard";
import { DATASET_PATH } from "@/lib/nav";
import { getIsSystemAdmin } from "@/lib/system-admin";
import { mapSource } from "@/lib/today";
import { DatasetMap } from "./dataset-map";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ map?: string }> };

/**
 * The whole dataset, for system admins (the owner, 2026-11-11): for now, a
 * map of every tenant's fish, whether or not the tenant has opted in to the
 * public page. Never finer than a broad region, like each tenant's map. Anyone
 * else finds no page here, as the API answers them.
 */
export default async function DatasetPage({ searchParams }: Props) {
  const source = mapSource((await searchParams).map);
  const session = await requireSignedIn(DATASET_PATH);
  const token = session.accessToken ?? "";
  if (!(await getIsSystemAdmin(token))) notFound();

  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-7 px-6 py-9">
      <header className="flex flex-col gap-1.5">
        <h1 className="text-3xl font-bold tracking-tight">Dataset</h1>
        <p className="text-fg-2">
          Every tenant&apos;s fish, including tenants that have not opted in to the public page. Only system admins
          see this page, and opening it is recorded for each tenant you are not a member of.
        </p>
      </header>

      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-xl font-semibold">Where we&apos;ve measured</h2>
          <div className="ml-auto">
            <MapSourceToggle base={DATASET_PATH} source={source} />
          </div>
        </div>
        {/* Keyed by the labels, so a switch shows the map loading again. */}
        <Suspense key={source} fallback={<MapLoading />}>
          <DatasetMap token={token} source={source} />
        </Suspense>
      </section>
    </main>
  );
}
