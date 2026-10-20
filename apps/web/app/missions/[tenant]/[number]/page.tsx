import Link from "next/link";
import { notFound } from "next/navigation";
import { getMyTenants } from "@/lib/fishsense-api";
import { isSlug } from "@/lib/ids";
import {
  getDeployments,
  getMission,
  purposeLabel,
  type Deployment,
} from "@/lib/mission-facts";
import { requireSignedIn } from "../../../captures/guard";
import { FactsForms } from "../../facts-forms";
import { deploymentHref, PortBadge } from "../../mission-table";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string; number: string }> };

/**
 * One mission (PLAN.md §4.9, Phase 2b): what it is for, its camera port and
 * what said so, where and when, and its field trip. A tenant's admins can
 * state its port, purpose and deployment here.
 */
export default async function MissionPage({ params }: Props) {
  const { tenant, number: rawNumber } = await params;
  if (!isSlug(tenant) || !/^\d+$/.test(rawNumber)) notFound();
  const number = Number(rawNumber);
  const session = await requireSignedIn(`/missions/${tenant}/${number}`);
  const token = session.accessToken ?? "";
  const [mission, tenants] = await Promise.all([
    getMission(token, tenant, number),
    getMyTenants(token),
  ]);
  if (!mission) notFound();
  const isAdmin = tenants.some((t) => t.slug === tenant && t.isAdmin);
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
          <span className="block text-xs text-slate-500">{mission.port_note}</span>
        )}
        {mission.port === "unknown" && (
          <span className="block text-xs text-amber-700 dark:text-amber-400">
            Nothing says which port this camera was behind. A missing D does not mean flat.
          </span>
        )}
        {!mission.processable && (
          <span className="block text-xs text-amber-700 dark:text-amber-400">
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
          className="text-sky-700 hover:underline dark:text-sky-400"
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
          <p className="text-sm text-slate-500">Mission {mission.number}</p>
          <h1 className="text-3xl font-semibold tracking-tight">
            {mission.name ?? `Mission ${mission.number}`}
          </h1>
        </div>
        <Link
          href={`/missions?tenant=${encodeURIComponent(tenant)}`}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
        >
          All missions
        </Link>
      </header>

      <dl className="mb-8 grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-[10rem_1fr]">
        {facts.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="font-medium text-slate-500">{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>

      {mission.notes && (
        <p className="mb-8 whitespace-pre-wrap text-sm text-slate-700 dark:text-slate-300">
          {mission.notes}
        </p>
      )}

      {isAdmin && <FactsForms slug={tenant} mission={mission} deployments={deployments} />}
    </main>
  );
}
