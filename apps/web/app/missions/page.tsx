import Link from "next/link";
import { chooseTenant } from "@/lib/captures";
import { getMyTenants, type TenantMembership } from "@/lib/fishsense-api";
import {
  getTenantMissions,
  PURPOSES,
  purposeLabel,
  type MissionRow,
  type Purpose,
} from "@/lib/mission-facts";
import { requireSignedIn } from "../captures/guard";
import { MissionTable } from "./mission-table";

export const dynamic = "force-dynamic";

type Props = {
  searchParams: Promise<{ tenant?: string; purpose?: string; port?: string }>;
};

type PurposeFilter = Purpose | "none" | undefined;

function purposeFilter(value: string | undefined): PurposeFilter {
  if (value === "none") return "none";
  return PURPOSES.find((p) => p === value);
}

function href(tenant: string, purpose?: PurposeFilter, unknownPort?: boolean): string {
  const query = new URLSearchParams({ tenant });
  if (purpose) query.set("purpose", purpose);
  if (unknownPort) query.set("port", "unknown");
  return `/missions?${query}`;
}

function keep(m: MissionRow, purpose: PurposeFilter, unknownPort: boolean): boolean {
  if (unknownPort && m.port !== "unknown") return false;
  if (purpose === "none") return m.purpose == null;
  return purpose === undefined || m.purpose === purpose;
}

const chip =
  "rounded-full border border-slate-300 px-3 py-1 text-sm aria-[current=page]:border-slate-900 aria-[current=page]:bg-slate-900 aria-[current=page]:text-white dark:border-slate-700 dark:aria-[current=page]:border-slate-100 dark:aria-[current=page]:bg-slate-100 dark:aria-[current=page]:text-slate-900";

/**
 * Every mission in a tenant -- Lite dives and calibration sessions alike --
 * with what each is for, its camera port and what said so, and its field
 * trip (PLAN.md §4.9, Phase 2b). Any member sees their tenant's.
 */
export default async function MissionsPage({ searchParams }: Props) {
  const { tenant: requested, purpose: rawPurpose, port } = await searchParams;
  const purpose = purposeFilter(rawPurpose);
  const unknownPort = port === "unknown";
  const query = new URLSearchParams();
  if (requested) query.set("tenant", requested);
  if (purpose) query.set("purpose", purpose);
  if (unknownPort) query.set("port", "unknown");
  const session = await requireSignedIn(`/missions${query.size ? `?${query}` : ""}`);
  const token = session.accessToken ?? "";

  let tenants: TenantMembership[] = [];
  let tenant: TenantMembership | null = null;
  let missions: MissionRow[] = [];
  let failure: string | null = null;
  try {
    tenants = await getMyTenants(token);
    tenant = chooseTenant(tenants, requested);
    if (tenant) missions = await getTenantMissions(token, tenant.slug);
  } catch (error) {
    failure = error instanceof Error ? error.message : "unavailable";
  }
  const shown = missions.filter((m) => keep(m, purpose, unknownPort));
  const unknownCount = missions.filter((m) => m.port === "unknown").length;

  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Missions</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            Every recording session: dives, calibrations and known-length runs.{" "}
            {tenant && (
              <Link href={`/deployments?tenant=${tenant.slug}`} className="text-sky-700 hover:underline dark:text-sky-400">
                Deployments
              </Link>
            )}
          </p>
        </div>
        {tenants.length > 1 && (
          <nav aria-label="Tenant" className="flex flex-wrap gap-2">
            {tenants.map((t) => (
              <Link
                key={t.slug}
                href={href(t.slug)}
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
        <div className="mb-6 space-y-3">
          <nav aria-label="Purpose" className="flex flex-wrap gap-2">
            <Link href={href(tenant.slug, undefined, unknownPort)} aria-current={purpose === undefined ? "page" : undefined} className={chip}>
              All
            </Link>
            {PURPOSES.map((p) => (
              <Link key={p} href={href(tenant.slug, p, unknownPort)} aria-current={purpose === p ? "page" : undefined} className={chip}>
                {purposeLabel(p)}
              </Link>
            ))}
            <Link href={href(tenant.slug, "none", unknownPort)} aria-current={purpose === "none" ? "page" : undefined} className={chip}>
              Not stated
            </Link>
          </nav>
          <nav aria-label="Port" className="flex flex-wrap items-center gap-2 text-sm">
            <Link href={href(tenant.slug, purpose, !unknownPort)} aria-current={unknownPort ? "page" : undefined} className={chip}>
              Unknown port only ({unknownCount})
            </Link>
          </nav>
        </div>
      )}

      {failure ? (
        <p className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-950 dark:text-red-300">
          Could not load missions: {failure}
        </p>
      ) : !tenant ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">
          This account isn&apos;t a member of any tenant yet.
        </p>
      ) : shown.length === 0 ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">No missions match.</p>
      ) : (
        <>
          <p className="mb-2 text-sm text-slate-500">
            {shown.length} of {missions.length} missions
          </p>
          <MissionTable slug={tenant.slug} missions={shown} />
        </>
      )}
    </main>
  );
}
