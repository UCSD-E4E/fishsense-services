import Link from "next/link";
import { chooseTenant, formatLength } from "@/lib/captures";
import { getMyTenants, type TenantMembership } from "@/lib/fishsense-api";
import {
  deviceFilter,
  fishName,
  formatError,
  getObservationPage,
  knownLengthError,
  methodLabel,
  observationHref,
  where,
  type Device,
  type Observation,
  type ObservationPage,
} from "@/lib/observations";
import { requireSignedIn } from "../captures/guard";

export const dynamic = "force-dynamic";

type Props = {
  searchParams: Promise<{ tenant?: string; device?: string; before?: string }>;
};

const FILTERS: { label: string; device?: Device }[] = [
  { label: "All" },
  { label: "FishSense Lite", device: "lite" },
  { label: "FishSense Mobile", device: "mobile" },
  { label: "Other devices", device: "other" },
];

function href(tenant: string, device?: Device, before?: string): string {
  const query = new URLSearchParams({ tenant });
  if (device) query.set("device", device);
  if (before) query.set("before", before);
  return `/fish?${query}`;
}

/**
 * Every measured fish in a tenant, Lite and Mobile together (PLAN.md §4.9,
 * Phase 1 of §9.26), newest first. Any member sees their tenant's.
 */
export default async function FishPage({ searchParams }: Props) {
  const { tenant: requested, device: rawDevice, before } = await searchParams;
  const device = deviceFilter(rawDevice);
  const query = new URLSearchParams();
  if (requested) query.set("tenant", requested);
  if (device) query.set("device", device);
  if (before) query.set("before", before);
  const session = await requireSignedIn(`/fish${query.size ? `?${query}` : ""}`);
  const token = session.accessToken ?? "";

  let tenants: TenantMembership[] = [];
  let tenant: TenantMembership | null = null;
  let page: ObservationPage | null = null;
  let failure: string | null = null;
  try {
    tenants = await getMyTenants(token);
    tenant = chooseTenant(tenants, requested);
    if (tenant) page = await getObservationPage(token, tenant.slug, { device, before });
  } catch (error) {
    failure = error instanceof Error ? error.message : "unavailable";
  }

  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Fish</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            Every measured fish, from FishSense Lite and FishSense Mobile.
          </p>
        </div>
        {tenants.length > 1 && (
          <nav aria-label="Tenant" className="flex flex-wrap gap-2">
            {tenants.map((t) => (
              <Link
                key={t.slug}
                href={href(t.slug, device)}
                aria-current={t.slug === tenant?.slug ? "page" : undefined}
                className="rounded-md border border-slate-300 px-3 py-1 text-sm aria-[current=page]:border-sky-600 aria-[current=page]:bg-sky-50 dark:border-slate-700 dark:aria-[current=page]:bg-sky-950"
              >
                {t.name}
              </Link>
            ))}
          </nav>
        )}
      </header>

      {tenant && (
        <nav aria-label="Device" className="mb-6 flex flex-wrap gap-2">
          {FILTERS.map((f) => (
            <Link
              key={f.label}
              href={href(tenant.slug, f.device)}
              aria-current={f.device === device ? "page" : undefined}
              className="rounded-full border border-slate-300 px-3 py-1 text-sm aria-[current=page]:border-slate-900 aria-[current=page]:bg-slate-900 aria-[current=page]:text-white dark:border-slate-700 dark:aria-[current=page]:border-slate-100 dark:aria-[current=page]:bg-slate-100 dark:aria-[current=page]:text-slate-900"
            >
              {f.label}
            </Link>
          ))}
        </nav>
      )}

      {failure ? (
        <p className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-950 dark:text-red-300">
          Could not load fish: {failure}
        </p>
      ) : !tenant ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">
          This account isn&apos;t a member of any tenant yet.
        </p>
      ) : page && page.observations.length === 0 ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">
          No measured fish from {tenant.name} yet.
        </p>
      ) : (
        <>
          <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {page?.observations.map((o) => (
              <li key={`${o.capture_id}/${o.fish_id}`}>
                <FishCard slug={tenant.slug} observation={o} />
              </li>
            ))}
          </ul>
          {page?.next && (
            <div className="mt-8">
              <Link
                href={href(tenant.slug, device, page.next)}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
              >
                Older fish
              </Link>
            </div>
          )}
        </>
      )}
    </main>
  );
}

function FishCard({ slug, observation: o }: { slug: string; observation: Observation }) {
  const fish = fishName(o);
  const error = knownLengthError(o);
  const place = where(o);
  return (
    <Link
      href={observationHref(slug, o)}
      className="block overflow-hidden rounded-lg border border-slate-200 bg-white shadow-xs transition hover:shadow-md dark:border-slate-800 dark:bg-slate-900"
    >
      {o.photo_url ? (
        // Presigned, short-lived Garage URLs: not for next/image's optimizer,
        // which would cache them past their expiry.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={o.photo_url}
          alt={fish.name}
          loading="lazy"
          className="aspect-4/3 w-full object-cover"
        />
      ) : (
        <div className="flex aspect-4/3 w-full items-center justify-center bg-slate-100 text-sm text-slate-500 dark:bg-slate-800 dark:text-slate-400">
          No photo yet
        </div>
      )}
      <div className="space-y-1 p-4 text-sm">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-lg font-medium">{formatLength(o.length_m)}</span>
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-700 dark:bg-slate-800 dark:text-slate-300">
            {methodLabel(o.method)}
            {o.device.name ? ` · ${o.device.name}` : ""}
          </span>
        </div>
        <div>
          {fish.name}
          {fish.detail && (
            <span className="text-slate-500">
              {" "}
              {o.species ? <i>{fish.detail}</i> : `(${fish.detail})`}
            </span>
          )}
        </div>
        {error !== null && (
          <div className="text-slate-500">
            {formatError(error)} against its known{" "}
            {formatLength(o.fish_model?.known_length_m)}
          </div>
        )}
        <div className="text-slate-500">
          {new Date(o.captured_at).toLocaleString()}
          {place ? ` · ${place}` : ""}
        </div>
      </div>
    </Link>
  );
}
