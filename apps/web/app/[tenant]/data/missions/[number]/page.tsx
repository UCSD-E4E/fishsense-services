import Link from "next/link";
import { notFound } from "next/navigation";
import { isSlug } from "@/lib/ids";
import {
  cameraLabel,
  getDeployments,
  isPhoneMission,
  getMission,
  purposeLabel,
  type Deployment,
} from "@/lib/mission-facts";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { CaptureGrid } from "@/app/[tenant]/data/missions/capture-grid";
import { frameLink, getMissionCaptures } from "@/lib/mission-captures";
import { getMissionAutomatic } from "@/lib/automatic";
import { AutomaticRun, hasRun } from "@/app/[tenant]/data/missions/automatic-run";
import { FactsForms } from "@/app/[tenant]/data/missions/facts-forms";
import { deploymentHref, PortBadge } from "@/app/[tenant]/data/missions/mission-table";
import { paths } from "@/lib/tenant-paths";

export const dynamic = "force-dynamic";

type Props = {
  params: Promise<{ tenant: string; number: string }>;
  searchParams: Promise<{ after?: string }>;
};

/**
 * One mission (PLAN.md §4.9, Phase 2b): what it is for, its camera port and
 * what said so, where and when, and its field trip. A tenant's admins can
 * state its port, purpose and deployment here, as can a system admin who has
 * unlocked the tenant (0056). Its captures are listed below its facts, measured
 * or not, a page at a time.
 */
export default async function MissionPage({ params, searchParams }: Props) {
  const { tenant, number: rawNumber } = await params;
  const { after } = await searchParams;
  if (!isSlug(tenant) || !/^\d+$/.test(rawNumber)) notFound();
  const number = Number(rawNumber);
  const session = await requireSignedIn(`${paths.missions(tenant)}/${number}`);
  const token = session.accessToken ?? "";
  const { isAdmin } = await tenantFor(token, tenant);
  const mission = await getMission(token, tenant, number);
  if (!mission) notFound();
  // Its captures, measured or not: an unmeasured one is seen here or nowhere.
  const captures = await getMissionCaptures(token, tenant, number, after);
  // The automatic run, for review (2026-10-21).
  const run = await getMissionAutomatic(token, tenant, number);
  const deployments: Deployment[] = isAdmin ? await getDeployments(token, tenant) : [];
  const place = [mission.site, mission.region, mission.country].filter(Boolean).join(", ");
  const phone = isPhoneMission(mission);
  const portFact: [string, React.ReactNode] = [
    "Camera port",
    <span key="port" className="space-y-1">
      <PortBadge port={mission.port} source={mission.port_source} />
      {mission.port_note && (
        <span className="block text-xs text-fg-2">{mission.port_note}</span>
      )}
      {mission.port === "unknown" && (
        <span className="block text-xs text-warn">
          Nothing says which port this camera was behind. A missing D does not mean flat.
        </span>
      )}
      {!mission.processable && (
        <span className="block text-xs text-warn">
          The pipeline won&apos;t process this mission: it needs the corrective optic and a
          pinhole camera calibration, and a borrowed calibration from a mission that has
          them too.
        </span>
      )}
    </span>,
  ];

  const facts: [string, React.ReactNode][] = [
    ["Started", new Date(mission.started_at).toLocaleString()],
    ["Device", cameraLabel(mission)],
    ["Purpose", purposeLabel(mission.purpose)],
    // A phone has no camera port and no folder (0057).
    ...(phone ? [] : [portFact]),
    [
      "Deployment",
      mission.deployment_number != null ? (
        <Link
          key="deployment"
          href={deploymentHref(tenant, mission.deployment_number)}
          className="text-teal-fg hover:underline"
        >
          {mission.deployment_name}
        </Link>
      ) : (
        "—"
      ),
    ],
    ["Place", place || "—"],
    [phone ? "Captures" : "Frames", mission.captures.toLocaleString()],
    ...(phone
      ? []
      : ([
          ["Priority", mission.priority],
          ["Folder", <code key="folder" className="break-all text-xs">{mission.source_path}</code>],
        ] as [string, React.ReactNode][])),
  ];

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-sm text-fg-2">Mission {mission.number}</p>
          <h1 className="text-3xl font-semibold tracking-tight">
            {mission.name ?? `Mission ${mission.number}`}
          </h1>
        </div>
        <Link
          href={paths.missions(tenant)}
          className="rounded-control border border-line px-3 py-1.5 text-sm "
        >
          All missions
        </Link>
      </header>

      <dl className="mb-8 grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-[10rem_1fr]">
        {facts.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="font-medium text-fg-2">{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>

      {mission.notes && (
        <p className="mb-8 whitespace-pre-wrap text-sm text-fg">
          {mission.notes}
        </p>
      )}

      {hasRun(run) && <AutomaticRun run={run} />}

      {captures && captures.captures.length > 0 && (
        <section className="mb-8">
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
            <h2 className="text-lg font-semibold">
              Captures{" "}
              <span className="text-sm font-normal text-fg-2">
                {mission.captures.toLocaleString()}, measured or not
              </span>
            </h2>
            {/* Review walks the frames in order, from the first (each frame
                page offers the next). */}
            {!after && (
              <Link
                href={frameLink(tenant, number, captures.captures[0])}
                className="text-sm font-medium text-teal-fg hover:underline"
              >
                Review frames →
              </Link>
            )}
          </div>
          <CaptureGrid slug={tenant} mission={number} captures={captures.captures} />
          <div className="mt-4 flex gap-4 text-sm">
            {after && (
              <Link href={paths.mission(tenant, number)} className="text-teal-fg hover:underline">
                Back to the first
              </Link>
            )}
            {captures.next && (
              <Link
                href={`${paths.mission(tenant, number)}?after=${encodeURIComponent(captures.next)}`}
                className="text-teal-fg hover:underline"
              >
                More captures
              </Link>
            )}
          </div>
        </section>
      )}

      {isAdmin && <FactsForms slug={tenant} mission={mission} deployments={deployments} />}
    </main>
  );
}
