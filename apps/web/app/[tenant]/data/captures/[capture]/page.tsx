import { notFound, redirect } from "next/navigation";
import { requireSignedIn } from "@/app/_shell/guard";
import { fishOfCapture, getCapture } from "@/lib/captures";
import { paths } from "@/lib/tenant-paths";

export const dynamic = "force-dynamic";

type Props = { params: Promise<{ tenant: string; capture: string }> };

/**
 * An old link to one FishSense Mobile capture. Its page was retired in phase
 * 7b: everything it showed is on its fish's page, which this sends it to.
 */
export default async function Page({ params }: Props) {
  const { tenant, capture: captureId } = await params;
  const session = await requireSignedIn(paths.capture(tenant, captureId));
  const capture = await getCapture(session.accessToken ?? "", tenant, captureId).catch(
    (error: unknown) => {
      // An id that can't be one is "not found", not a crash.
      if (error instanceof Error && /^Invalid /.test(error.message)) return null;
      throw error;
    },
  );
  const fish = capture ? fishOfCapture(tenant, capture) : null;
  if (!fish) notFound();
  redirect(fish);
}
