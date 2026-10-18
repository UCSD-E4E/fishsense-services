import Link from "next/link";
import {
  chooseTenant,
  formatLength,
  getCapturePage,
  placeName,
  type CapturePage,
} from "@/lib/captures";
import { getMyTenants, type TenantMembership } from "@/lib/fishsense-api";
import { requireSignedIn } from "./guard";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ tenant?: string; before?: string }> };

/**
 * FishSense Mobile captures (new in v2): what members of a tenant synced from
 * their phones, newest first. Any member sees their tenant's captures; a
 * member of several picks one.
 */
export default async function CapturesPage({ searchParams }: Props) {
  const { tenant: requested, before } = await searchParams;
  const query = new URLSearchParams();
  if (requested) query.set("tenant", requested);
  if (before) query.set("before", before);
  const session = await requireSignedIn(
    `/captures${query.size ? `?${query}` : ""}`,
  );
  const token = session.accessToken ?? "";

  let tenants: TenantMembership[] = [];
  let tenant: TenantMembership | null = null;
  let page: CapturePage | null = null;
  let failure: string | null = null;
  try {
    tenants = await getMyTenants(token);
    tenant = chooseTenant(tenants, requested);
    if (tenant) page = await getCapturePage(token, tenant.slug, before);
  } catch (error) {
    failure = error instanceof Error ? error.message : "unavailable";
  }

  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Mobile captures
          </h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            Fish measured with FishSense Mobile and synced from the phone.
          </p>
        </div>
        {tenants.length > 1 && (
          <nav aria-label="Tenant" className="flex flex-wrap gap-2">
            {tenants.map((t) => (
              <Link
                key={t.slug}
                href={`/captures?tenant=${encodeURIComponent(t.slug)}`}
                aria-current={t.slug === tenant?.slug ? "page" : undefined}
                className="rounded-md border border-slate-300 px-3 py-1 text-sm aria-[current=page]:border-sky-600 aria-[current=page]:bg-sky-50 dark:border-slate-700 dark:aria-[current=page]:bg-sky-950"
              >
                {t.name}
              </Link>
            ))}
          </nav>
        )}
      </header>

      {failure ? (
        <p className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-950 dark:text-red-300">
          Could not load captures: {failure}
        </p>
      ) : !tenant ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">
          This account isn&apos;t a member of any tenant yet.
        </p>
      ) : page && page.captures.length === 0 ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">
          No captures from {tenant.name} yet.
        </p>
      ) : (
        <>
          <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {page?.captures.map((c) => (
              <li key={c.client_capture_uuid}>
                <Link
                  href={`/captures/${tenant.slug}/${c.client_capture_uuid}`}
                  className="block overflow-hidden rounded-lg border border-slate-200 bg-white shadow-xs transition hover:shadow-md dark:border-slate-800 dark:bg-slate-900"
                >
                  {c.rgb_url && (
                    // Presigned, short-lived Garage URLs: not for next/image's
                    // optimizer, which would cache them past their expiry.
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={c.rgb_url}
                      alt="Fish capture"
                      loading="lazy"
                      className="aspect-4/3 w-full object-cover"
                    />
                  )}
                  <div className="p-4 text-sm">
                    <div className="text-lg font-medium">
                      {formatLength(c.length_m)}
                    </div>
                    <div className="text-slate-500">
                      {new Date(c.captured_at).toLocaleString()}
                    </div>
                    <Place capture={c} />
                  </div>
                </Link>
              </li>
            ))}
          </ul>
          {page?.next && (
            <div className="mt-8">
              <Link
                href={`/captures?tenant=${encodeURIComponent(tenant.slug)}&before=${encodeURIComponent(page.next)}`}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
              >
                Older captures
              </Link>
            </div>
          )}
        </>
      )}
    </main>
  );
}

function Place({ capture }: { capture: Parameters<typeof placeName>[0] }) {
  const place = placeName(capture);
  if (!place) return null;
  return (
    <div className="text-slate-500">
      {place.name}
      {place.derived ? " (from coordinates)" : ""}
    </div>
  );
}
