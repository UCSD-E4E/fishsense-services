import Link from "next/link";
import { notFound } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { tenantFor } from "@/app/_shell/tenant";
import { AutomaticSection } from "@/app/[tenant]/fish/[capture]/[fish]/automatic-section";
import { getAutomaticFrame } from "@/lib/automatic";
import { isSlug, isUuid } from "@/lib/ids";
import { frameLink, getMissionCaptures, parseCaptureCursor } from "@/lib/mission-captures";
import { getMission } from "@/lib/mission-facts";
import { getCaptureReviews, getMySkills } from "@/lib/reviews";
import { paths } from "@/lib/tenant-paths";
import { ReviewWorkspace } from "./review-workspace";

export const dynamic = "force-dynamic";

type Props = {
  params: Promise<{ tenant: string; number: string; capture: string }>;
  searchParams: Promise<{ at?: string }>;
};

/**
 * One frame of a mission, measured or not (2026-10-21): what the automatic
 * run made of it, drawn on the render it was predicted on, and its review
 * (2026-10-22): the frame page is review's home, not the Labelling tab. `at`
 * is the frame's place in the mission's order (the API's page cursor), which
 * the mission's frame cards carry, so the page can offer the next frame.
 */
export default async function FramePage({ params, searchParams }: Props) {
  const { tenant, number: rawNumber, capture } = await params;
  if (!isSlug(tenant) || !/^\d+$/.test(rawNumber) || !isUuid(capture)) notFound();
  const number = Number(rawNumber);
  const at = parseCaptureCursor((await searchParams).at);
  const framePath = paths.frame(tenant, number, capture);
  const session = await requireSignedIn(framePath);
  const token = session.accessToken ?? "";
  const pageTenant = await tenantFor(token, tenant);
  const [mission, frame, reviews, skills, after] = await Promise.all([
    getMission(token, tenant, number),
    getAutomaticFrame(token, tenant, capture),
    getCaptureReviews(token, tenant, capture),
    getMySkills(token, tenant),
    at ? getMissionCaptures(token, tenant, number, at, 1) : null,
  ]);
  if (!mission) notFound();
  const next = after?.captures[0];
  const nextHref = next ? frameLink(tenant, number, next) : null;

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
        <AutomaticSection
          frame={frame}
          humanLengthM={null}
          figure={
            <ReviewWorkspace
              tenant={tenant}
              captureId={capture}
              framePath={framePath}
              frame={frame}
              reviews={reviews}
              skills={skills}
              readOnly={pageTenant.access === "read_only"}
              maskSrc={frame.mask_url ? `/api/masks/${tenant}/${capture}` : null}
              nextHref={nextHref}
            />
          }
        />
      ) : (
        <>
          <p className="rounded-card border border-dashed border-line bg-raised p-6 text-sm text-fg-2">
            The automatic run hasn&apos;t reached this frame, and nobody has measured it.
          </p>
          {nextHref && (
            <p className="mt-3 text-right">
              <Link href={nextHref} className="text-sm font-medium text-teal-fg hover:underline">
                Next frame →
              </Link>
            </p>
          )}
        </>
      )}
    </main>
  );
}
