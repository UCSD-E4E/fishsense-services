import Link from "next/link";
import { redirect, unstable_rethrow } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { formatLength, millimetres } from "@/lib/captures";
import { type TenantMembership } from "@/lib/fishsense-api";
import { pageCount, pageLinks, pageRange, parsePage } from "@/lib/pages";
import { paths } from "@/lib/tenant-paths";
import { SpeciesSelect } from "./species-select";
import {
  deviceFilter,
  fishName,
  formatError,
  getObservationPage,
  getObservationSpecies,
  speciesFilter,
  knownLengthError,
  methodLabel,
  observationHref,
  where,
  type Device,
  type Observation,
  type ObservationPage,
  type SpeciesCount,
} from "@/lib/observations";

export const dynamic = "force-dynamic";

type Props = {
  params: Promise<{ tenant: string }>;
  searchParams: Promise<{ device?: string; species?: string; page?: string }>;
};

const PER_PAGE = 50;

const FILTERS: { label: string; device?: Device }[] = [
  { label: "All" },
  { label: "Lite · laser", device: "lite" },
  { label: "Mobile · LiDAR", device: "mobile" },
  { label: "Other devices", device: "other" },
];

/** The page's address: its device and species filters, and its page. */
type Filters = { device?: Device; species?: string };

function href(tenant: string, { device, species }: Filters, page = 1): string {
  const query = new URLSearchParams();
  if (device) query.set("device", device);
  if (species) query.set("species", species);
  if (page > 1) query.set("page", String(page));
  return `${paths.fish(tenant)}${query.size ? `?${query}` : ""}`;
}

/**
 * Every measured fish in a tenant, Lite and Mobile together (PLAN.md §4.9,
 * Phase 1 of §9.26), newest first. Any member sees their tenant's. Fifty to
 * a page, by number (`?page=`), with how many there are in all.
 */
export default async function FishPage({ params, searchParams }: Props) {
  const { tenant: slug } = await params;
  const { device: rawDevice, species: rawSpecies, page: rawPage } = await searchParams;
  const device = deviceFilter(rawDevice);
  const species = speciesFilter(rawSpecies);
  const filters = { device, species };
  const current = parsePage(rawPage);
  const session = await requireSignedIn(href(slug, filters, current));
  const token = session.accessToken ?? "";

  let tenant: TenantMembership | null = null;
  let page: ObservationPage | null = null;
  let choices: SpeciesCount[] = [];
  let failure: string | null = null;
  try {
    tenant = await tenantFor(token, slug);
    [page, choices] = await Promise.all([
      getObservationPage(token, tenant.slug, { device, species, page: current, limit: PER_PAGE }),
      getObservationSpecies(token, tenant.slug, device),
    ]);
  } catch (error) {
    unstable_rethrow(error);
    failure = error instanceof Error ? error.message : "unavailable";
  }
  const last = page ? pageCount(page.total, PER_PAGE) : 1;
  // Past the end (a bookmark from when there were more, or a typed number):
  // the last page there is.
  if (tenant && page && page.observations.length === 0 && current > last) {
    redirect(href(tenant.slug, filters, last));
  }
  const range = page ? pageRange(current, PER_PAGE, page.total) : null;

  return (
    <main className="mx-auto flex max-w-7xl flex-col gap-7 px-6 py-10">
      <header className="flex flex-col gap-1.5">
        <h1 className="text-3xl font-bold tracking-tight">Fish</h1>
        <p className="text-fg-2">
          Every measured fish{tenant ? ` in ${tenant.name}` : ""}, from FishSense Lite and
          FishSense Mobile.
        </p>
      </header>

      {tenant && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <nav aria-label="Device" className="flex flex-wrap gap-2">
            {FILTERS.map((f) => (
              <Link
                key={f.label}
                href={href(tenant.slug, { device: f.device, species })}
                aria-current={f.device === device ? "page" : undefined}
                className="inline-flex h-11 items-center rounded-chip border border-line bg-surface px-4 text-sm font-medium aria-[current=page]:border-teal aria-[current=page]:bg-teal-tint aria-[current=page]:font-semibold aria-[current=page]:text-teal-fg"
              >
                {f.label}
              </Link>
            ))}
          </nav>
          {/* Only the species this tenant has (the owner, 2026-10-23). */}
          <SpeciesSelect
            species={choices}
            current={species}
            hrefFor={Object.fromEntries(
              ["", ...choices.map((c) => c.scientific_name), ...(species ? [species] : [])].map((name) => [
                name,
                href(tenant.slug, { device, species: name || undefined }),
              ]),
            )}
          />
        </div>
      )}

      {failure ? (
        <p role="alert" className="rounded-card border border-error/40 bg-error/10 p-4 text-sm text-error">
          Could not load fish: {failure}
        </p>
      ) : !tenant ? (
        <p className="text-fg-2">This account isn&apos;t a member of any tenant yet.</p>
      ) : page && page.observations.length === 0 ? (
        <p className="text-fg-2">
          {species || device ? "No measured fish match these filters." : `No measured fish from ${tenant.name} yet.`}
        </p>
      ) : (
        <>
          {page && range && (
            <p className="text-sm text-fg-2 tabular-nums">
              {range.first.toLocaleString()}–{range.last.toLocaleString()} of {page.total.toLocaleString()}
            </p>
          )}
          <ul className="grid gap-5 [grid-template-columns:repeat(auto-fill,minmax(260px,1fr))]">
            {page?.observations.map((o) => (
              <li key={`${o.capture_id}/${o.fish_id}`}>
                <FishCard slug={tenant.slug} observation={o} />
              </li>
            ))}
          </ul>
          {last > 1 && <Pager slug={tenant.slug} filters={filters} current={current} last={last} />}
        </>
      )}
    </main>
  );
}

function Pager({
  slug,
  filters,
  current,
  last,
}: {
  slug: string;
  filters: Filters;
  current: number;
  last: number;
}) {
  const box = "inline-flex h-11 min-w-11 items-center justify-center rounded-control px-3 font-medium tabular-nums";
  const link = `${box} border border-line bg-surface hover:bg-raised`;
  const off = `${box} border border-line text-fg-3`;
  return (
    <nav aria-label="Pages" className="flex flex-wrap items-center justify-center gap-2">
      {current > 1 ? (
        <Link href={href(slug, filters, current - 1)} rel="prev" className={link}>
          ‹ Newer
        </Link>
      ) : (
        <span aria-disabled className={off}>
          ‹ Newer
        </span>
      )}
      {pageLinks(current, last).map((n, i) =>
        n === "gap" ? (
          <span key={`gap-${i}`} aria-hidden className="px-1 text-fg-2">
            …
          </span>
        ) : n === current ? (
          <span
            key={n}
            aria-current="page"
            className={`${box} border border-teal bg-teal-tint font-semibold text-teal-fg`}
          >
            {n}
          </span>
        ) : (
          <Link key={n} href={href(slug, filters, n)} aria-label={`Page ${n}`} className={link}>
            {n}
          </Link>
        ),
      )}
      {current < last ? (
        <Link href={href(slug, filters, current + 1)} rel="next" className={link}>
          Older ›
        </Link>
      ) : (
        <span aria-disabled className={off}>
          Older ›
        </span>
      )}
    </nav>
  );
}

function FishCard({ slug, observation: o }: { slug: string; observation: Observation }) {
  const fish = fishName(o);
  const error = knownLengthError(o);
  const place = where(o);
  return (
    <Link
      href={observationHref(slug, o)}
      className="flex h-full flex-col overflow-hidden rounded-card border border-line bg-surface transition hover:border-fg-3"
    >
      {o.photo_url ? (
        // Presigned, short-lived Garage URLs: not for next/image's optimizer,
        // which would cache them past their expiry.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={o.photo_url}
          alt={fish.name}
          loading="lazy"
          className="aspect-4/3 w-full border-b border-line object-cover"
        />
      ) : (
        <div className="flex aspect-4/3 w-full items-center justify-center border-b border-dashed border-line bg-raised text-sm text-fg-2">
          No photo yet
        </div>
      )}
      <div className="flex flex-col gap-1.5 p-4">
        <div className="flex items-baseline justify-between gap-3">
          <span className="font-semibold">{fish.name}</span>
          <span className="whitespace-nowrap text-[22px] font-semibold tabular-nums">
            {millimetres(o.length_m)}
            {o.length_m != null && <span className="text-sm font-medium text-fg-2"> mm</span>}
          </span>
        </div>
        {fish.detail && (
          <div className="text-sm text-fg-2">
            {o.species ? <i>{fish.detail}</i> : fish.detail}
          </div>
        )}
        {error !== null && (
          <div className="text-sm text-fg-2">
            {formatError(error)} against its known {formatLength(o.fish_model?.known_length_m)}
          </div>
        )}
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="inline-flex h-6 items-center rounded-chip bg-raised px-2 text-xs font-medium">
            {methodLabel(o.method)}
            {o.device.name ? ` · ${o.device.name}` : ""}
          </span>
          <span className="text-xs text-fg-2">
            {new Date(o.captured_at).toLocaleDateString()}
            {place ? ` · ${place}` : ""}
          </span>
        </div>
      </div>
    </Link>
  );
}
