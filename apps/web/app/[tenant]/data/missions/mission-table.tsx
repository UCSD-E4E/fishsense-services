import Link from "next/link";
import {
  cameraLabel,
  isPhoneMission,
  portLabel,
  purposeLabel,
  type MissionRow,
} from "@/lib/mission-facts";
import { paths } from "@/lib/tenant-paths";

export const missionHref = paths.mission;
export const deploymentHref = paths.deployment;

/** Missions as rows: what each is for, its port and what said so, its camera
 *  and frames, and its trip. */
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
    <div className="overflow-x-auto rounded-card border border-line">
      <table className="w-full text-left text-sm">
        <thead className="bg-raised text-xs uppercase tracking-wide text-fg-2">
          <tr>
            <th className="px-3 py-2">#</th>
            <th className="px-3 py-2">Mission</th>
            <th className="px-3 py-2">Started</th>
            <th className="px-3 py-2">Purpose</th>
            <th className="px-3 py-2">Port</th>
            <th className="px-3 py-2">Camera</th>
            <th className="px-3 py-2 text-right">Frames</th>
            {showDeployment && <th className="px-3 py-2">Deployment</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {missions.map((m) => (
            <tr key={m.number} className="hover:bg-raised">
              <td className="px-3 py-2 tabular-nums text-fg-2">{m.number}</td>
              <td className="px-3 py-2">
                <Link href={missionHref(slug, m.number)} className="font-medium hover:underline">
                  {m.name ?? `Mission ${m.number}`}
                </Link>
                {m.site && <span className="text-fg-2"> · {m.site}</span>}
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-fg-2">
                {new Date(m.started_at).toLocaleDateString()}
              </td>
              <td className="px-3 py-2">
                <span className={m.purpose ? "" : "text-fg-3"}>
                  {purposeLabel(m.purpose)}
                </span>
              </td>
              <td className="px-3 py-2">
                {isPhoneMission(m) ? (
                  <span className="text-fg-3">—</span>
                ) : (
                  <PortBadge port={m.port} source={m.port_source} />
                )}
              </td>
              <td className="px-3 py-2 text-fg-2">{cameraLabel(m)}</td>
              <td className="px-3 py-2 text-right tabular-nums text-fg-2">
                {m.captures.toLocaleString()}
              </td>
              {showDeployment && (
                <td className="px-3 py-2">
                  {m.deployment_number != null ? (
                    <Link
                      href={deploymentHref(slug, m.deployment_number)}
                      className="text-teal-fg hover:underline"
                    >
                      {m.deployment_name}
                    </Link>
                  ) : (
                    <span className="text-fg-3">—</span>
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
      ? "bg-warn/10 text-warn ring-warn/50"
      : "bg-raised text-fg ring-line";
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs ring-1 ${tone}`}>
      {portLabel(port, source)}
    </span>
  );
}
