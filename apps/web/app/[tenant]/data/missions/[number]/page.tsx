import Link from "next/link";
import { notFound } from "next/navigation";
import { isSlug } from "@/lib/ids";
import {
  getDeployments,
  getMission,
  purposeLabel,
  type Deployment,
} from "@/lib/mission-facts";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { FactsForms } from "@/app/[tenant]/data/missions/facts-forms";
import { deploymentHref, PortBadge } from "@/app/[tenant]/data/missions/mission-table";
import { paths } from "@/lib/tenant-paths";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string; number: string }> };

/**
 * One mission (PLAN.md §4.9, Phase 2b): what it is for, its camera port and
 * what said so, where and when, and its field trip. A tenant's admins can
 * state its port, purpose and deployment here, as can a system admin who has
 * unlocked the tenant (0056).
 */
export default async function MissionPage({ params }: Props) {
  const { tenant, number: rawNumber } = await params;
  if (!isSlug(tenant) || !/^\d+$/.test(rawNumber)) notFound();
  const number = Number(rawNumber);
  const session = await requireSignedIn(`${paths.missions(tenant)}/${number}`);
  const token = session.accessToken ?? "";
  const { isAdmin } = await tenantFor(token, tenant);
  const mission = await getMission(token, tenant, number);
  if (!mission) notFound();
  const deployments: Deployment[] = isAdmin ? await getDeployments(token, tenant) : [];
  const place = [mission.site, mission.region, mission.country].filter(Boolean).join(", ");

  const facts: [string, React.ReactNode][] = [
    ["Started", new Date(mission.started_at).toLocaleString()],
    ["Device", mission.device_name ?? "—"],
    ["Purpose", purposeLabel(mission.purpose)],
    [
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
    ],
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
    ["Frames", mission.captures.toLocaleString()],
    ["Priority", mission.priority],
    ["Folder", <code key="folder" className="break-all text-xs">{mission.source_path}</code>],
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

      {isAdmin && <FactsForms slug={tenant} mission={mission} deployments={deployments} />}
    </main>
  );
}
