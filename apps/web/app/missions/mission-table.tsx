import Link from "next/link";
import { portLabel, purposeLabel, type MissionRow } from "@/lib/mission-facts";

export function missionHref(slug: string, number: number): string {
  return `/missions/${slug}/${number}`;
}

export function deploymentHref(slug: string, number: number): string {
  return `/deployments/${slug}/${number}`;
}

/** Missions as rows: what each is for, its port and what said so, its trip. */
export function MissionTable({
  slug,
  missions,
  showDeployment = true,
}: {
  slug: string;
  missions: MissionRow[];
  showDeployment?: boolean;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
      <table className="w-full text-left text-sm">
        <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
          <tr>
            <th className="px-3 py-2">#</th>
            <th className="px-3 py-2">Mission</th>
            <th className="px-3 py-2">Started</th>
            <th className="px-3 py-2">Purpose</th>
            <th className="px-3 py-2">Port</th>
            {showDeployment && <th className="px-3 py-2">Deployment</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
          {missions.map((m) => (
            <tr key={m.number} className="hover:bg-slate-50 dark:hover:bg-slate-900">
              <td className="px-3 py-2 tabular-nums text-slate-500">{m.number}</td>
              <td className="px-3 py-2">
                <Link href={missionHref(slug, m.number)} className="font-medium hover:underline">
                  {m.name ?? `Mission ${m.number}`}
                </Link>
                {m.site && <span className="text-slate-500"> · {m.site}</span>}
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-slate-600 dark:text-slate-400">
                {new Date(m.started_at).toLocaleDateString()}
              </td>
              <td className="px-3 py-2">
                <span className={m.purpose ? "" : "text-slate-400"}>
                  {purposeLabel(m.purpose)}
                </span>
              </td>
              <td className="px-3 py-2">
                <PortBadge port={m.port} source={m.port_source} />
              </td>
              {showDeployment && (
                <td className="px-3 py-2">
                  {m.deployment_number != null ? (
                    <Link
                      href={deploymentHref(slug, m.deployment_number)}
                      className="text-sky-700 hover:underline dark:text-sky-400"
                    >
                      {m.deployment_name}
                    </Link>
                  ) : (
                    <span className="text-slate-400">—</span>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PortBadge({
  port,
  source,
}: {
  port: MissionRow["port"];
  source: MissionRow["port_source"];
}) {
  const tone =
    port === "unknown"
      ? "bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-800"
      : "bg-slate-100 text-slate-700 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700";
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs ring-1 ${tone}`}>
      {portLabel(port, source)}
    </span>
  );
}
