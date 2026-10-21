import Link from "next/link";
import { unstable_rethrow } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { type TenantMembership } from "@/lib/fishsense-api";
import { paths } from "@/lib/tenant-paths";
import {
  sourceFilter,
  type Source,
  portUnknown,
  getTenantMissions,
  PURPOSES,
  purposeLabel,
  type MissionRow,
  type Purpose,
} from "@/lib/mission-facts";
import { MissionTable } from "./mission-table";

export const dynamic = "force-dynamic";

type Props = {
  params: Promise<{ tenant: string }>;
  searchParams: Promise<{ purpose?: string; port?: string; source?: string }>;
};

type PurposeFilter = Purpose | "none" | undefined;

function purposeFilter(value: string | undefined): PurposeFilter {
  if (value === "none") return "none";
  return PURPOSES.find((p) => p === value);
}

type Filters = { purpose?: PurposeFilter; unknownPort?: boolean; source?: Source };

function href(tenant: string, { purpose, unknownPort, source }: Filters): string {
  const query = new URLSearchParams();
  if (source) query.set("source", source);
  if (purpose) query.set("purpose", purpose);
  if (unknownPort) query.set("port", "unknown");
  return `${paths.missions(tenant)}${query.size ? `?${query}` : ""}`;
}

function keep(m: MissionRow, { purpose, unknownPort, source }: Filters): boolean {
  if (source && m.source !== source) return false;
  if (unknownPort && !portUnknown(m)) return false;
  if (purpose === "none") return m.purpose == null;
  return purpose === undefined || m.purpose === purpose;
}

const chip =
  "inline-flex h-11 items-center rounded-chip border border-line bg-surface px-4 text-sm font-medium aria-[current=page]:border-teal aria-[current=page]:bg-teal-tint aria-[current=page]:font-semibold aria-[current=page]:text-teal-fg";

/**
 * Every mission in a tenant -- Lite dives, calibration sessions and phone
 * outings (0057) alike --
 * with what each is for, its camera port and what said so, and its field
 * trip (PLAN.md §4.9, Phase 2b). Any member sees their tenant's.
 */
export default async function MissionsPage({ params, searchParams }: Props) {
  const { tenant: slug } = await params;
  const { purpose: rawPurpose, port, source: rawSource } = await searchParams;
  const filters: Filters = {
    purpose: purposeFilter(rawPurpose),
    unknownPort: port === "unknown",
    source: sourceFilter(rawSource),
  };
  const { purpose, unknownPort, source } = filters;
  const session = await requireSignedIn(href(slug, filters));
  const token = session.accessToken ?? "";

  let tenant: TenantMembership | null = null;
  let missions: MissionRow[] = [];
  let failure: string | null = null;
  try {
    tenant = await tenantFor(token, slug);
    missions = await getTenantMissions(token, tenant.slug);
  } catch (error) {
    unstable_rethrow(error);
    failure = error instanceof Error ? error.message : "unavailable";
  }
  const shown = missions.filter((m) => keep(m, filters));
  const unknownCount = missions.filter(portUnknown).length;

  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Missions</h1>
          <p className="mt-1 text-sm text-fg-2">
            Every recording session: dives, calibrations and known-length runs.{" "}
            {tenant && (
              <Link href={paths.data(tenant.slug)} className="text-teal-fg hover:underline">
                Deployments
              </Link>
            )}
          </p>
        </div>
      </header>

      {tenant && (
        <div className="mb-6 space-y-3">
          <nav aria-label="Source" className="flex flex-wrap gap-2">
            <Link href={href(tenant.slug, { ...filters, source: undefined })} aria-current={source === undefined ? "page" : undefined} className={chip}>
              Lite and phone
            </Link>
            <Link href={href(tenant.slug, { ...filters, source: "lite" })} aria-current={source === "lite" ? "page" : undefined} className={chip}>
              Lite
            </Link>
            <Link href={href(tenant.slug, { ...filters, source: "mobile" })} aria-current={source === "mobile" ? "page" : undefined} className={chip}>
              Phone
            </Link>
          </nav>
          <nav aria-label="Purpose" className="flex flex-wrap gap-2">
            <Link href={href(tenant.slug, { ...filters, purpose: undefined })} aria-current={purpose === undefined ? "page" : undefined} className={chip}>
              All
            </Link>
            {PURPOSES.map((p) => (
              <Link key={p} href={href(tenant.slug, { ...filters, purpose: p })} aria-current={purpose === p ? "page" : undefined} className={chip}>
                {purposeLabel(p)}
              </Link>
            ))}
            <Link href={href(tenant.slug, { ...filters, purpose: "none" })} aria-current={purpose === "none" ? "page" : undefined} className={chip}>
              Not stated
            </Link>
          </nav>
          <nav aria-label="Port" className="flex flex-wrap items-center gap-2 text-sm">
            <Link href={href(tenant.slug, { ...filters, unknownPort: !unknownPort })} aria-current={unknownPort ? "page" : undefined} className={chip}>
              Unknown port only ({unknownCount})
            </Link>
          </nav>
        </div>
      )}

      {failure ? (
        <p className="rounded-card border border-error/40 bg-error/10 p-3 text-sm text-error">
          Could not load missions: {failure}
        </p>
      ) : !tenant ? (
        <p className="text-sm text-fg-2">
          This account isn&apos;t a member of any tenant yet.
        </p>
      ) : shown.length === 0 ? (
        <p className="text-sm text-fg-2">No missions match.</p>
      ) : (
        <>
          <p className="mb-2 text-sm text-fg-2">
            {shown.length} of {missions.length} missions
          </p>
          <MissionTable slug={tenant.slug} missions={shown} />
        </>
      )}
    </main>
  );
}
