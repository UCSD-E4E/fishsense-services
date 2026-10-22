import Link from "next/link";
import { notFound } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { AutomaticSection } from "@/app/[tenant]/fish/[capture]/[fish]/automatic-section";
import { getAutomaticFrame } from "@/lib/automatic";
import { isSlug, isUuid } from "@/lib/ids";
import { getMission } from "@/lib/mission-facts";
import { paths } from "@/lib/tenant-paths";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string; number: string; capture: string }> };

/**
 * One frame of a mission, measured or not (2026-10-21): what the automatic
 * run made of it, drawn on the render it was predicted on. A measured frame's
 * fish page shows the same section beside the measurement; this is how a
 * frame nobody measured is seen, and where its review will live.
 */
export default async function FramePage({ params }: Props) {
  const { tenant, number: rawNumber, capture } = await params;
  if (!isSlug(tenant) || !/^\d+$/.test(rawNumber) || !isUuid(capture)) notFound();
  const number = Number(rawNumber);
  const session = await requireSignedIn(paths.frame(tenant, number, capture));
  const token = session.accessToken ?? "";
  await tenantFor(token, tenant);
  const [mission, frame] = await Promise.all([
    getMission(token, tenant, number),
    getAutomaticFrame(token, tenant, capture),
  ]);
  if (!mission) notFound();

  return (
    <main className="mx-auto max-w-7xl px-6 py-8">
      <nav aria-label="Breadcrumb" className="mb-6 text-sm text-fg-2">
        <Link href={paths.missions(tenant)} className="text-teal-fg hover:underline">
          Missions
        </Link>{" "}
        <span aria-hidden>›</span>{" "}
        <Link href={paths.mission(tenant, number)} className="text-teal-fg hover:underline">
          {mission.name ?? `Mission ${number}`}
        </Link>{" "}
        <span aria-hidden>›</span> Frame
      </nav>
      {frame ? (
        <AutomaticSection frame={frame} humanLengthM={null} />
      ) : (
        <p className="rounded-card border border-dashed border-line bg-raised p-6 text-sm text-fg-2">
          The automatic run hasn&apos;t reached this frame, and nobody has measured it.
        </p>
      )}
    </main>
  );
}
