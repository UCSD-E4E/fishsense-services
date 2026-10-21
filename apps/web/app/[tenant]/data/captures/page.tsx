import Link from "next/link";
import { unstable_rethrow } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { memberOf, myTenants } from "@/app/_shell/tenant";
import { type TenantMembership } from "@/lib/fishsense-api";
import { paths } from "@/lib/tenant-paths";
import {
  formatLength,
  getCapturePage,
  placeName,
  type CapturePage,
} from "@/lib/captures";

export const dynamic = "force-dynamic";

type Props = {
  params: Promise<{ tenant: string }>;
  searchParams: Promise<{ before?: string }>;
};

/**
 * FishSense Mobile captures (new in v2): what members of a tenant synced from
 * their phones, newest first. Any member sees their tenant's captures; a
 * member of several picks one.
 */
export default async function CapturesPage({ params, searchParams }: Props) {
  const { tenant: slug } = await params;
  const { before } = await searchParams;
  const here = `${paths.captures(slug)}${before ? `?before=${encodeURIComponent(before)}` : ""}`;
  const session = await requireSignedIn(here);
  const token = session.accessToken ?? "";

  let tenant: TenantMembership | null = null;
  let page: CapturePage | null = null;
  let failure: string | null = null;
  try {
    tenant = memberOf(await myTenants(token), slug);
    page = await getCapturePage(token, tenant.slug, before);
  } catch (error) {
    unstable_rethrow(error);
    failure = error instanceof Error ? error.message : "unavailable";
  }

  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">
            Mobile captures
          </h1>
          <p className="mt-1 text-sm text-fg-2">
            Fish measured with FishSense Mobile and synced from the phone.
          </p>
        </div>
      </header>

      {failure ? (
        <p className="rounded-card border border-error/40 bg-error/10 p-3 text-sm text-error">
          Could not load captures: {failure}
        </p>
      ) : !tenant ? (
        <p className="text-sm text-fg-2">
          This account isn&apos;t a member of any tenant yet.
        </p>
      ) : page && page.captures.length === 0 ? (
        <p className="text-sm text-fg-2">
          No captures from {tenant.name} yet.
        </p>
      ) : (
        <>
          <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {page?.captures.map((c) => (
              <li key={c.client_capture_uuid}>
                <Link
                  href={paths.capture(tenant.slug, c.client_capture_uuid)}
                  className="block overflow-hidden rounded-card border border-line bg-surface transition hover:border-fg-3"
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
                    <div className="text-fg-2">
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
                href={`${paths.captures(tenant.slug)}?before=${encodeURIComponent(page.next)}`}
                className="rounded-control border border-line px-3 py-1.5 text-sm "
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
    <div className="text-fg-2">
      {place.name}
      {place.derived ? " (from coordinates)" : ""}
    </div>
  );
}
