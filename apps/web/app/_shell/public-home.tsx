import Link from "next/link";
import { getPublicSummary, project, regionLabel, type PublicSummary } from "@/lib/public-page";
import { signInHref } from "@/lib/shell";

/**
 * The signed-out home (docs/design/README.md, Home pages; the owner,
 * 2026-10-20): what FishSense does, live totals, and wild fish by broad
 * region, from tenants that opted in. Nothing finer than a region is ever on
 * it: the API decides what may be said (`public_store`).
 */
export async function PublicHome() {
  let summary: PublicSummary | null = null;
  try {
    summary = await getPublicSummary();
  } catch (error) {
    console.error("public home: the public summary failed", error);
  }

  return (
    <main className="flex flex-col">
      <header className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-6 pb-10 pt-16">
        <h1 className="max-w-4xl text-5xl font-bold leading-[1.05] tracking-tight sm:text-6xl">
          A fish&apos;s length, from a photo.
        </h1>
        <p className="max-w-2xl text-lg leading-relaxed text-fg-2">
          Underwater, a dive camera with a calibrated laser measures fish as they swim. On deck or
          on the shore, a phone&apos;s LiDAR measures the fish in your hands. Either way, every
          length keeps the photo and the evidence behind it.
        </p>
        <div className="mt-2 flex flex-wrap gap-3">
          <Link
            href={signInHref("/", "")}
            prefetch={false}
            className="inline-flex h-12 items-center rounded-card bg-teal px-5 font-semibold text-on-teal"
          >
            Sign in
          </Link>
          <a
            href="#how"
            className="inline-flex h-12 items-center rounded-card border border-line bg-surface px-5 font-medium"
          >
            How it works
          </a>
        </div>
      </header>

      {summary && (
        <section aria-labelledby="map-title" className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-6 pb-14">
          <div className="flex flex-wrap items-baseline gap-3">
            <h2 id="map-title" className="text-xl font-semibold">
              Where FishSense has measured
            </h2>
            <span className="text-sm text-fg-2">
              Wild fish by broad region, rounded. No sites, no coordinates.
            </span>
          </div>
          <div className="relative overflow-hidden rounded-sheet border border-line bg-surface">
            <div
              aria-hidden
              className="aspect-[1200/473] w-full bg-raised"
              style={{
                maskImage: "url(/world.svg)",
                WebkitMaskImage: "url(/world.svg)",
                maskSize: "100% 100%",
                WebkitMaskSize: "100% 100%",
              }}
            />
            {summary.regions.map((r) => {
              const { left, top } = project(r.latitude, r.longitude);
              return (
                <div
                  key={r.name}
                  className="absolute flex -translate-x-1/2 -translate-y-full flex-col items-center"
                  style={{ left: `${left}%`, top: `${top}%` }}
                >
                  <div className="flex flex-col whitespace-nowrap rounded-control border border-teal bg-surface px-3 py-2 shadow-sm">
                    <span className="text-xs text-fg-2">{r.name}</span>
                    <span className="font-semibold tabular-nums">{regionLabel(r)}</span>
                  </div>
                  <svg viewBox="0 0 22 30" className="h-7 w-5 fill-teal" aria-hidden>
                    <path d="M11 29s9-9.5 9-17a9 9 0 0 0-18 0c0 7.5 9 17 9 17z" />
                    <circle cx="11" cy="12" r="3.5" className="fill-ground" />
                  </svg>
                </div>
              );
            })}
            {summary.regions.length === 0 && (
              <p className="absolute inset-0 flex items-center justify-center text-fg-2">
                Measurements will appear here by region.
              </p>
            )}
          </div>
          <p className="text-sm text-fg-2">
            A region shows a number once it holds {summary.minimum} wild fish; until then it says
            &ldquo;under {summary.minimum}&rdquo;. Pool trials of fish models count in the totals
            below, not on the map.
          </p>
          <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
            <Total value={summary.lengths} label="lengths measured" />
            <Total value={summary.wild_fish} label={`wild fish, ${summary.species} species`} />
            <Total value={summary.model_lengths} label="fish-model lengths in pool trials" />
            <Total value={summary.deployments} label={`trips, ${summary.missions.toLocaleString()} sessions`} />
          </div>
        </section>
      )}

      <section id="how" aria-labelledby="how-title" className="border-t border-line bg-surface">
        <div className="mx-auto flex max-w-7xl flex-col gap-7 px-6 py-16">
          <h2 id="how-title" className="text-3xl font-bold">
            Two ways to measure
          </h2>
          <div className="grid gap-6 [grid-template-columns:repeat(auto-fit,minmax(320px,1fr))]">
            <Way title="FishSense Lite">
              A dive camera behind a corrective optic, with a laser whose spot gives the distance to
              the fish. Photos come back from the dive and are measured here.
            </Way>
            <Way title="FishSense Mobile">
              A phone with LiDAR measures a fish you have caught, right where you are, and syncs the
              photo, the depth and the length when you are back in signal.
            </Way>
          </div>
        </div>
      </section>
    </main>
  );
}

function Total({ value, label }: { value: number; label: string }) {
  return (
    <div className="rounded-card border border-line bg-surface p-5">
      <div className="text-4xl font-semibold tracking-tight tabular-nums">{value.toLocaleString()}</div>
      <div className="mt-1 text-sm text-fg-2">{label}</div>
    </div>
  );
}

function Way({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-3 rounded-sheet border border-line bg-ground p-7">
      <h3 className="text-xl font-semibold">{title}</h3>
      <p className="leading-relaxed text-fg-2">{children}</p>
    </div>
  );
}
